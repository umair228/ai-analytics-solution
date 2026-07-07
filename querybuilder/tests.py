"""Executor tests: filter-WHERE compilation and full-data explore queries."""
import os
import sqlite3
import tempfile

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from connections.models import DataSource
from querybuilder.executor import (
    QueryError,
    _dialect_error_hint,
    _remap_order_by,
    _wrap_for_filter,
    build_filter_where,
    execute_dataset_query,
    execute_raw_sql_filtered,
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

    def test_resolve_maps_columns_to_aliases(self):
        where, _ = build_filter_where(
            [{"column": "VALUE", "type": "compare", "op": ">=", "value": 10}],
            self.COLS, _quote, resolve=lambda c: '"dse_c1"')
        self.assertEqual(where, '"dse_c1" >= :flt0')


class DialectHintTests(SimpleTestCase):
    def test_hint_for_tsql_on_sqlite(self):
        for q in ("SELECT TRY_CAST(x AS DATETIME) FROM t",
                  "SELECT DATEDIFF(MINUTE, a, b) FROM t",
                  "SELECT CONVERT(varchar, x) FROM t",
                  "SELECT ISNULL(x, 0) FROM t"):
            self.assertIn("SQLite", _dialect_error_hint(q, "sqlite"))
            self.assertIn("julianday", _dialect_error_hint(q, "sqlite"))

    def test_no_hint_for_plain_sql_on_sqlite(self):
        self.assertEqual(_dialect_error_hint("SELECT date(x) FROM t", "sqlite"), "")

    def test_no_hint_on_mssql(self):
        self.assertEqual(
            _dialect_error_hint("SELECT TRY_CAST(x AS DATETIME) FROM t", "mssql"), "")


class RemapOrderByTests(SimpleTestCase):
    NAME_TO_SQL = {"ENTERED_ON": '"ENTERED_ON"', "VALUE": '"VALUE"'}

    def test_strips_table_qualifier(self):
        self.assertEqual(
            _remap_order_by("ORDER BY R.ENTERED_ON DESC", self.NAME_TO_SQL),
            'ORDER BY "ENTERED_ON" DESC')

    def test_multi_term(self):
        self.assertEqual(
            _remap_order_by("ORDER BY R.ENTERED_ON DESC, S.VALUE", self.NAME_TO_SQL),
            'ORDER BY "ENTERED_ON" DESC, "VALUE"')

    def test_drops_when_column_not_in_output(self):
        self.assertEqual(_remap_order_by("ORDER BY R.MISSING", self.NAME_TO_SQL), "")

    def test_drops_expressions(self):
        self.assertEqual(_remap_order_by("ORDER BY LEN(R.NAME) DESC", self.NAME_TO_SQL), "")

    def test_empty(self):
        self.assertEqual(_remap_order_by("", self.NAME_TO_SQL), "")


class WrapForFilterTests(SimpleTestCase):
    def test_plain_wrapper_without_dups(self):
        _, n2s, proj, aliased = _wrap_for_filter("SELECT a, b", ["A", "B"], _quote, "sqlite")
        self.assertEqual(proj, "*")
        self.assertFalse(aliased)
        self.assertEqual(n2s["A"], '"A"')

    def test_alias_list_for_mssql_dups(self):
        fc, n2s, proj, aliased = _wrap_for_filter(
            "SELECT a, b, a", ["NAME", "B", "NAME"], _quote, "mssql")
        self.assertTrue(aliased)
        self.assertIn('AS dse_sub ("dse_c0", "dse_c1", "dse_c2")', fc)
        self.assertEqual(n2s["NAME"], '"dse_c0"')  # first occurrence
        self.assertEqual(proj, '"dse_c0", "dse_c1", "dse_c2"')

    def test_dedup_suffix_triggers_alias_on_mssql(self):
        # The driver renders the duplicate as "NAME:1" — still needs aliasing.
        _, _, _, aliased = _wrap_for_filter(
            "SELECT a, b, a", ["NAME", "B", "NAME:1"], _quote, "mssql")
        self.assertTrue(aliased)

    def test_mssql_normal_query_uses_plain_wrapper(self):
        _, _, proj, aliased = _wrap_for_filter(
            "SELECT a, b", ["A", "B"], _quote, "mssql")
        self.assertFalse(aliased)
        self.assertEqual(proj, "*")

    def test_sqlite_dups_use_plain_wrapper(self):
        # SQLite tolerates duplicate subquery columns, so no alias list needed.
        _, _, proj, aliased = _wrap_for_filter(
            "SELECT a, b, a", ["NAME", "B", "NAME:1"], _quote, "sqlite")
        self.assertFalse(aliased)
        self.assertEqual(proj, "*")


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

    def test_scalar(self):
        out = self._run({"mode": "scalar", "y": "VALUE", "agg": "avg"})
        self.assertAlmostEqual(out["value"], 31.1)
        out = self._run(
            {"mode": "scalar", "agg": "count"},
            filters=[{"column": "PRODUCT", "type": "dropdown", "value": "Diesel"}])
        self.assertEqual(out["value"], 3)

    def test_aggregate_bucketed_by_month(self):
        out = self._run({"mode": "aggregate", "x": "ENTERED_ON", "y": "VALUE",
                         "agg": "sum", "x_bucket": "month"})
        self.assertEqual(out["x_bucket"], "month")
        # Chronological, day-collapsed buckets.
        self.assertEqual([d["label"] for d in out["data"]],
                         ["2022-08", "2023-01", "2023-08"])
        self.assertEqual(out["data"][0]["value"], 60.0)

    def test_aggregate_bucketed_by_year(self):
        out = self._run({"mode": "aggregate", "x": "ENTERED_ON",
                         "agg": "count", "x_bucket": "year"})
        self.assertEqual(
            {d["label"]: d["value"] for d in out["data"]},
            {"2022": 3, "2023": 2})

    def test_rejects_unknown_identifiers(self):
        with self.assertRaises(QueryError):
            self._run({"mode": "aggregate", "x": "NOPE", "agg": "count"})
        with self.assertRaises(QueryError):
            self._run({"mode": "aggregate", "x": "PRODUCT", "y": "VALUE",
                       "agg": "median"})
        with self.assertRaises(QueryError):
            self._run({"mode": "extent", "column": "NOPE"})


class DuplicateColumnQueryTests(TestCase):
    """Regression: real LIMS queries that SELECT a column twice + carry a
    table-qualified trailing ORDER BY (both break a naive derived-table wrap)."""

    # Mirrors the prod query shape: product selected twice, ORDER BY r.entered_on.
    # SQLAlchemy dedups the second duplicate key to "product:1" (what the app's
    # cached_columns actually stores).
    SQL = ("SELECT r.entered_on, r.product, r.value, r.product "
           "FROM results r ORDER BY r.entered_on DESC")
    COLS = ["entered_on", "product", "value", "product:1"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = sqlite3.connect(cls.db_path)
        conn.execute("CREATE TABLE results (entered_on TEXT, product TEXT, value REAL)")
        conn.executemany("INSERT INTO results VALUES (?, ?, ?)", [
            ("2024-02-08 00:50:09", "JET_A1", 255.0),
            ("2024-02-08 00:43:42", "GASOIL", 1.5),
            ("2024-02-07 09:00:00", "JET_A1", 42.0),
            ("2024-01-10 08:00:00", "LPG", 24.0),
        ])
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.unlink(cls.db_path)

    def setUp(self):
        owner = User.objects.create_user(username="dup-tester", password="x")
        self.ds = DataSource.objects.create(
            name="dup-sqlite", source_type="sqlite", owner=owner,
            options={"path": self.db_path})

    def _run(self, spec, filters=None):
        return execute_dataset_query(
            self.ds, self.SQL, filters=filters, columns=self.COLS, spec=spec)

    def test_count_with_date_filter_does_not_error(self):
        out = self._run(
            {"mode": "count"},
            filters=[{"column": "entered_on", "type": "date-range",
                      "from": "2024-02-08", "to": "2024-02-08"}])
        self.assertEqual(out["total_row_count"], 2)

    def test_rows_preserve_duplicate_names_and_order(self):
        out = self._run({"mode": "rows", "limit": 10})
        self.assertEqual(out["columns"], self.COLS)  # duplicate 'product' kept
        self.assertEqual(out["total_row_count"], 4)
        # Qualified ORDER BY r.entered_on DESC is remapped, not dropped.
        self.assertEqual(out["rows"][0][0], "2024-02-08 00:50:09")

    def test_aggregate_on_dup_column(self):
        out = self._run({"mode": "aggregate", "x": "product", "agg": "count"})
        self.assertEqual({d["label"]: d["value"] for d in out["data"]},
                         {"JET_A1": 2, "GASOIL": 1, "LPG": 1})

    def test_filtered_raw_sql_does_not_error(self):
        result = execute_raw_sql_filtered(
            self.ds, self.SQL, columns=self.COLS,
            filters=[{"column": "product", "type": "dropdown", "value": "JET_A1"}])
        self.assertEqual(result["columns"], self.COLS)
        self.assertEqual(result["row_count"], 2)
        self.assertTrue(all(r[1] == "JET_A1" for r in result["rows"]))
