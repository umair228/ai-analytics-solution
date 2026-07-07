"""Executor tests: filter-WHERE compilation and full-data explore queries."""
import os
import sqlite3
import tempfile

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from connections.models import DataSource
from querybuilder.executor import (
    QueryError,
    build_filter_where,
    execute_dataset_query,
)

User = get_user_model()

_quote = '"{}"'.format


class BuildFilterWhereTests(SimpleTestCase):
    COLS = ["PRODUCT", "VALUE", "ENTERED_ON"]

    def test_compare_ops_map_to_sql(self):
        where, params = build_filter_where(
            [{"column": "VALUE", "type": "compare", "op": ">=", "value": 10},
             {"column": "VALUE", "type": "compare", "op": "!=", "value": 5}],
            self.COLS, _quote)
        self.assertEqual(where, '"VALUE" >= :flt0 AND "VALUE" <> :flt1')
        self.assertEqual(params, {"flt0": 10, "flt1": 5})

    def test_compare_rejects_unknown_op(self):
        where, params = build_filter_where(
            [{"column": "VALUE", "type": "compare", "op": "LIKE", "value": "x"}],
            self.COLS, _quote)
        self.assertEqual((where, params), ("", {}))

    def test_dropdown_negation(self):
        where, _ = build_filter_where(
            [{"column": "PRODUCT", "type": "dropdown", "value": "Diesel", "not": True}],
            self.COLS, _quote)
        self.assertEqual(where, '"PRODUCT" <> :flt0')
        where, _ = build_filter_where(
            [{"column": "PRODUCT", "type": "dropdown",
              "values": ["A", "B"], "not": True}],
            self.COLS, _quote)
        self.assertEqual(where, '"PRODUCT" NOT IN (:flt0_0, :flt0_1)')

    def test_unlisted_column_is_ignored(self):
        where, params = build_filter_where(
            [{"column": "EVIL; DROP TABLE x", "type": "compare",
              "op": "=", "value": 1}],
            self.COLS, _quote)
        self.assertEqual((where, params), ("", {}))


class ExecuteDatasetQueryTests(TestCase):
    """End-to-end explore queries against a real SQLite file."""

    SQL = ("SELECT entered_on AS ENTERED_ON, product AS PRODUCT, "
           "value AS VALUE FROM results ORDER BY entered_on")
    COLS = ["ENTERED_ON", "PRODUCT", "VALUE"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = sqlite3.connect(cls.db_path)
        conn.execute(
            "CREATE TABLE results (entered_on TEXT, product TEXT, value REAL)")
        rows = [
            ("2022-08-01 12:35:35", "Diesel", 10.0),
            ("2022-08-15 09:00:00", "Diesel", 20.0),
            ("2022-08-30 23:10:00", "Naphtha", 30.0),
            ("2023-01-10 08:00:00", "Kerosene", 40.0),
            ("2023-08-24 12:35:35", "Diesel", 55.5),
        ]
        conn.executemany("INSERT INTO results VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.unlink(cls.db_path)

    def setUp(self):
        owner = User.objects.create_user(username="qb-tester", password="x")
        self.ds = DataSource.objects.create(
            name="test-sqlite", source_type="sqlite", owner=owner,
            options={"path": self.db_path})

    def _run(self, spec, filters=None):
        return execute_dataset_query(
            self.ds, self.SQL, filters=filters, columns=self.COLS, spec=spec)

    def test_count_full_and_filtered(self):
        self.assertEqual(self._run({"mode": "count"})["total_row_count"], 5)
        out = self._run(
            {"mode": "count"},
            filters=[{"column": "ENTERED_ON", "type": "date-range",
                      "from": "2022-08-01", "to": "2022-08-30"}])
        self.assertEqual(out["total_row_count"], 3)

    def test_extent(self):
        out = self._run({"mode": "extent", "column": "ENTERED_ON"})
        self.assertEqual(out["min"], "2022-08-01 12:35:35")
        self.assertEqual(out["max"], "2023-08-24 12:35:35")

    def test_aggregate_sum_by_product(self):
        out = self._run({"mode": "aggregate", "x": "PRODUCT", "y": "VALUE",
                         "agg": "sum"})
        data = {d["label"]: d["value"] for d in out["data"]}
        self.assertEqual(data, {"Diesel": 85.5, "Naphtha": 30.0,
                                "Kerosene": 40.0})
        # Ordered by value descending.
        self.assertEqual(out["data"][0]["label"], "Diesel")

    def test_aggregate_with_filters_and_group(self):
        out = self._run(
            {"mode": "aggregate", "x": "PRODUCT", "agg": "count",
             "group_by": "PRODUCT"},
            filters=[{"column": "PRODUCT", "type": "dropdown",
                      "values": ["Diesel", "Naphtha"]}])
        total = sum(d["value"] for d in out["data"])
        self.assertEqual(total, 4)
        self.assertTrue(all("group" in d for d in out["data"]))

    def test_rows_pagination_and_total(self):
        page1 = self._run({"mode": "rows", "limit": 2, "offset": 0})
        page2 = self._run({"mode": "rows", "limit": 2, "offset": 2})
        self.assertEqual(page1["total_row_count"], 5)
        self.assertEqual(len(page1["rows"]), 2)
        self.assertEqual(len(page2["rows"]), 2)
        self.assertNotEqual(page1["rows"][0], page2["rows"][0])
        self.assertEqual(page1["columns"], self.COLS)

    def test_compare_filter_on_measure(self):
        out = self._run(
            {"mode": "count"},
            filters=[{"column": "VALUE", "type": "compare",
                      "op": ">", "value": 25}])
        self.assertEqual(out["total_row_count"], 3)

    def test_rejects_unknown_identifiers(self):
        with self.assertRaises(QueryError):
            self._run({"mode": "aggregate", "x": "NOPE", "agg": "count"})
        with self.assertRaises(QueryError):
            self._run({"mode": "aggregate", "x": "PRODUCT", "y": "VALUE",
                       "agg": "median"})
        with self.assertRaises(QueryError):
            self._run({"mode": "extent", "column": "NOPE"})
