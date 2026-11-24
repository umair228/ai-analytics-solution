"""Register pages: server-side sort and multi-column search over a dataset's full result."""
import os
import sqlite3
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase

from connections.models import DataSource
from querybuilder.executor import QueryError, execute_dataset_query

SQL = "SELECT doc, project, stage, days FROM docs ORDER BY project, doc"
COLS = ["doc", "project", "stage", "days"]


class RegisterQueryTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fd, cls.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        con = sqlite3.connect(cls.path)
        con.execute("CREATE TABLE docs (doc TEXT, project TEXT, stage TEXT, days INTEGER)")
        con.executemany("INSERT INTO docs VALUES (?, ?, ?, ?)", [
            ("MISA-2419-001", "2419 · Pressure Vessel", "With client", 5),
            ("MISA-2419-002", "2419 · Pressure Vessel", "Not started", 40),
            ("SA-2418-100", "2418 · HDPE units", "With client", 5),
            ("SA-2418-101", "2418 · HDPE units", "Done", None),
            ("REF-50%-A", "2392 · Incinerator", "Not started", 12),
            ("REF_X", "2392 · Incinerator", "Done", 1),
        ])
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.unlink(cls.path)

    def setUp(self):
        owner = get_user_model().objects.create_user("reg", password="x")
        self.ds = DataSource.objects.create(name="reg", source_type="sqlite", owner=owner,
                                            options={"path": self.path})

    def rows(self, filters=None, **spec):
        out = execute_dataset_query(self.ds, SQL, filters=filters, columns=COLS,
                                    spec={"mode": "rows", **spec})
        return [r[0] for r in out["rows"]], out["total_row_count"]

    def search(self, text, columns=("doc", "project")):
        return [{"type": "any-text", "columns": list(columns), "value": text}]

    def test_search_matches_every_word_in_any_column_any_case(self):
        docs, n = self.rows(self.search("vessel 2419"))
        self.assertEqual((sorted(docs), n), (["MISA-2419-001", "MISA-2419-002"], 2))
        self.assertEqual(self.rows(self.search("HDPE SA-2418-1"))[1], 2)
        self.assertEqual(self.rows(self.search("nothing-like-it"))[1], 0)

    def test_wildcards_typed_in_the_search_are_matched_literally(self):
        self.assertEqual(self.rows(self.search("50%"))[0], ["REF-50%-A"])
        self.assertEqual(self.rows(self.search("ref_"))[0], ["REF_X"])   # "_" is not "any"

    def test_search_ignores_columns_the_dataset_does_not_have(self):
        self.assertEqual(self.rows(self.search("2419", columns=["nope"]))[1], 6)

    def test_sort_by_a_column_then_the_query_order_keeps_pages_stable(self):
        docs, _ = self.rows(sort=[{"column": "days", "dir": "desc"}])
        self.assertEqual(docs[0], "MISA-2419-002")
        # Ties (5 days) fall back to the query's own order: project, then doc.
        tied = [d for d in docs if d in ("MISA-2419-001", "SA-2418-100")]
        self.assertEqual(tied, ["SA-2418-100", "MISA-2419-001"])
        first, _ = self.rows(sort=[{"column": "stage", "dir": "asc"}], limit=3, offset=0)
        second, _ = self.rows(sort=[{"column": "stage", "dir": "asc"}], limit=3, offset=3)
        self.assertEqual(len(set(first) | set(second)), 6)        # whole, no repeats

    def test_an_unknown_sort_column_is_refused(self):
        with self.assertRaises(QueryError):
            self.rows(sort=[{"column": "days; DROP TABLE docs", "dir": "asc"}])


class FilterSplitTests(TestCase):
    def test_a_search_is_kept_while_any_of_its_columns_exists(self):
        from querybuilder.executor import split_filters
        f = [{"type": "any-text", "columns": ["doc", "gone"], "value": "x"},
             {"column": "stage", "type": "dropdown", "value": "Done"},
             {"column": "gone", "type": "dropdown", "value": "y"},
             {"type": "any-text", "columns": ["gone"], "value": "z"}]
        kept, skipped = split_filters(f, ["doc", "stage"])
        self.assertEqual([k.get("column") or k["type"] for k in kept], ["any-text", "stage"])
        self.assertEqual(skipped, ["gone"])
