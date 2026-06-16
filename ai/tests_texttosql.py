"""Unit tests for the Phase-1 text-to-SQL helpers.

Pure functions only — no DB, no LLM (SimpleTestCase). The schema builder, the
generator and the runner are exercised end-to-end by ``manage.py eval_text2sql``
against a live datasource (which needs the DB + LLM and so isn't a unit test).
"""
import os
import tempfile
import time
from unittest import mock

import pandas as pd
from sqlalchemy import create_engine
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from querybuilder.executor import QueryError
from texttosql import execution as texec
from texttosql.schema import SchemaContext, build_schema
from texttosql.sql_tools import (
    POSTGRES, MSSQL, ORACLE, SQLITE, ODBC,
    dialect_label, extract_sql, inject_row_limit,
)
from texttosql.validator import (
    cte_names, groupby_columns, is_aggregate, referenced_tables,
    sanity_violations, validate_sql,
)


def _ctx():
    return SchemaContext(
        datasource_name="EGPC", source_type="mssql",
        tables={
            "SAMPLE": [{"name": "SAMPLE_NUMBER", "type": "int"},
                       {"name": "PRODUCT", "type": "varchar"}],
            "RESULT": [{"name": "SAMPLE_NUMBER", "type": "int"},
                       {"name": "IN_SPEC", "type": "char"}],
            "TEST": [{"name": "INSTRUMENT", "type": "varchar"}],
        },
        do_not_group={"INSTRUMENT", "ASSIGNED_OPERATOR"},
        notes="NOTES HERE", total_tables=3)


class ExtractSqlTests(SimpleTestCase):
    def test_plain_select(self):
        self.assertEqual(extract_sql("SELECT 1"), "SELECT 1")

    def test_strips_fence(self):
        self.assertEqual(
            extract_sql("```sql\nSELECT a FROM t\n```"), "SELECT a FROM t")

    def test_drops_leading_prose(self):
        self.assertEqual(
            extract_sql("Here is the query:\nSELECT a FROM t WHERE x=1"),
            "SELECT a FROM t WHERE x=1")

    def test_keeps_only_first_statement(self):
        self.assertEqual(extract_sql("SELECT 1; DROP TABLE t;"), "SELECT 1")

    def test_with_cte(self):
        self.assertTrue(extract_sql("WITH c AS (SELECT 1) SELECT * FROM c").startswith("WITH"))

    def test_empty(self):
        self.assertEqual(extract_sql(""), "")
        self.assertEqual(extract_sql("sorry, no idea"), "")


class InjectRowLimitTests(SimpleTestCase):
    def test_mssql_adds_top(self):
        self.assertEqual(
            inject_row_limit("SELECT a FROM t", MSSQL, 50), "SELECT TOP 50 a FROM t")

    def test_mssql_top_after_distinct(self):
        self.assertEqual(
            inject_row_limit("SELECT DISTINCT a FROM t", MSSQL, 50),
            "SELECT DISTINCT TOP 50 a FROM t")

    def test_mssql_skips_existing_top(self):
        sql = "SELECT TOP 10 a FROM t"
        self.assertEqual(inject_row_limit(sql, MSSQL, 50), sql)

    def test_mssql_leaves_cte_alone(self):
        sql = "WITH c AS (SELECT a FROM t) SELECT * FROM c"
        self.assertEqual(inject_row_limit(sql, MSSQL, 50), sql)

    def test_postgres_appends_limit(self):
        self.assertEqual(
            inject_row_limit("SELECT a FROM t ORDER BY a", POSTGRES, 50),
            "SELECT a FROM t ORDER BY a\nLIMIT 50")

    def test_postgres_skips_existing_limit(self):
        sql = "SELECT a FROM t LIMIT 5"
        self.assertEqual(inject_row_limit(sql, POSTGRES, 50), sql)

    def test_oracle_fetch_first(self):
        self.assertTrue(
            inject_row_limit("SELECT a FROM t", ORACLE, 50).endswith("FETCH FIRST 50 ROWS ONLY"))

    def test_sqlite_limit(self):
        self.assertTrue(inject_row_limit("SELECT a FROM t", SQLITE, 50).endswith("LIMIT 50"))

    def test_odbc_unknown_left_alone(self):
        sql = "SELECT a FROM t"
        self.assertEqual(inject_row_limit(sql, ODBC, 50), sql)

    def test_strips_trailing_semicolon(self):
        self.assertEqual(inject_row_limit("SELECT a FROM t;", MSSQL, 5), "SELECT TOP 5 a FROM t")

    def test_mssql_literal_top_does_not_skip_cap(self):
        # "top" inside a string literal must NOT be read as an existing TOP clause
        out = inject_row_limit("SELECT a FROM t WHERE x = 'top 5'", MSSQL, 50)
        self.assertEqual(out, "SELECT TOP 50 a FROM t WHERE x = 'top 5'")

    def test_mssql_union_is_left_alone(self):
        sql = "SELECT a FROM t UNION SELECT b FROM u"
        self.assertEqual(inject_row_limit(sql, MSSQL, 50), sql)

    def test_postgres_literal_limit_still_appends(self):
        out = inject_row_limit("SELECT a FROM t WHERE x = 'limit 5'", POSTGRES, 50)
        self.assertTrue(out.endswith("LIMIT 50"))

    def test_postgres_trailing_limit_with_offset_skipped(self):
        sql = "SELECT a FROM t LIMIT 10 OFFSET 5"
        self.assertEqual(inject_row_limit(sql, POSTGRES, 50), sql)


class DialectLabelTests(SimpleTestCase):
    def test_known(self):
        self.assertIn("T-SQL", dialect_label(MSSQL))
        self.assertIn("LIMIT", dialect_label(POSTGRES))

    def test_unknown_falls_back(self):
        self.assertEqual(dialect_label("weirddb"), "generic SQL")


class IdentifierExtractionTests(SimpleTestCase):
    def test_referenced_tables_strips_schema_and_alias(self):
        sql = "SELECT * FROM dbo.SAMPLE s JOIN dbo.RESULT r ON r.SAMPLE_NUMBER=s.SAMPLE_NUMBER"
        self.assertEqual(referenced_tables(sql), {"sample", "result"})

    def test_cte_names(self):
        sql = "WITH oos AS (SELECT 1), x AS (SELECT 2) SELECT * FROM oos"
        self.assertEqual(cte_names(sql), {"oos", "x"})

    def test_cte_names_empty_without_with(self):
        self.assertEqual(cte_names("SELECT * FROM SAMPLE"), set())

    def test_groupby_columns(self):
        sql = "SELECT s.PRODUCT, r.ANALYSIS, COUNT(*) FROM RESULT r GROUP BY s.PRODUCT, r.ANALYSIS ORDER BY 3"
        self.assertEqual(groupby_columns(sql), ["product", "analysis"])

    def test_groupby_positional_ignored(self):
        self.assertEqual(groupby_columns("SELECT a, COUNT(*) FROM t GROUP BY 1"), [])

    def test_extract_from_is_not_a_table(self):
        # EXTRACT(EPOCH FROM col) must NOT read `col` as a table
        sql = "SELECT EXTRACT(YEAR FROM login_date) yr FROM SAMPLE GROUP BY EXTRACT(YEAR FROM login_date)"
        self.assertEqual(referenced_tables(sql), {"sample"})

    def test_from_inside_string_literal_ignored(self):
        self.assertEqual(referenced_tables("SELECT * FROM SAMPLE WHERE note = 'shipped from cairo'"),
                         {"sample"})

    def test_subquery_inner_table_found(self):
        sql = "SELECT * FROM (SELECT * FROM RESULT) x"
        self.assertEqual(referenced_tables(sql), {"result"})

    def test_comma_join_extracts_all_relations(self):
        sql = "SELECT * FROM SAMPLE s, RESULT r WHERE s.SAMPLE_NUMBER=r.SAMPLE_NUMBER"
        self.assertEqual(referenced_tables(sql), {"sample", "result"})

    def test_select_list_commas_are_not_tables(self):
        # the classic false-positive: SELECT a, b, c must NOT yield tables b/c
        self.assertEqual(referenced_tables("SELECT a, b, c FROM SAMPLE"), {"sample"})

    def test_subquery_select_list_commas_not_tables(self):
        self.assertEqual(referenced_tables("SELECT * FROM (SELECT a, b FROM RESULT) x"),
                         {"result"})

    def test_bracketed_and_quoted_tables(self):
        self.assertEqual(referenced_tables("SELECT * FROM [SAMPLE] s"), {"sample"})
        self.assertEqual(referenced_tables('SELECT * FROM "RESULT" r'), {"result"})

    def test_cross_apply_relation(self):
        self.assertEqual(referenced_tables("SELECT * FROM SAMPLE s CROSS APPLY RESULT r"),
                         {"sample", "result"})


class ValidateSqlTests(SimpleTestCase):
    def test_valid_query_passes(self):
        sql = ("SELECT s.PRODUCT, COUNT(*) FROM RESULT r JOIN SAMPLE s "
               "ON r.SAMPLE_NUMBER=s.SAMPLE_NUMBER WHERE r.IN_SPEC='F' GROUP BY s.PRODUCT")
        self.assertEqual(validate_sql(sql, _ctx()), [])

    def test_unknown_table_rejected(self):
        v = validate_sql("SELECT * FROM WIDGETS", _ctx())
        self.assertTrue(any("Unknown table" in x and "widgets" in x for x in v))

    def test_group_by_do_not_group_rejected(self):
        v = validate_sql("SELECT INSTRUMENT, COUNT(*) FROM TEST GROUP BY INSTRUMENT", _ctx())
        self.assertTrue(any("Do NOT GROUP BY" in x and "instrument" in x for x in v))

    def test_non_select_rejected(self):
        v = validate_sql("DELETE FROM SAMPLE", _ctx())
        self.assertTrue(v and "read-only" in v[0])

    def test_cte_target_allowed(self):
        sql = "WITH oos AS (SELECT PRODUCT FROM SAMPLE) SELECT * FROM oos"
        self.assertEqual(validate_sql(sql, _ctx()), [])

    def test_comma_join_hallucinated_table_rejected(self):
        v = validate_sql("SELECT * FROM SAMPLE, WIDGETS w WHERE w.id=1", _ctx())
        self.assertTrue(any("Unknown table" in x and "widgets" in x for x in v))

    def test_bracketed_hallucinated_table_rejected(self):
        v = validate_sql("SELECT * FROM [WIDGETS]", _ctx())
        self.assertTrue(any("Unknown table" in x and "widgets" in x for x in v))


class SanityGateTests(SimpleTestCase):
    def test_zero_row_aggregate_flagged(self):
        sql = "SELECT PRODUCT, COUNT(*) c FROM SAMPLE GROUP BY PRODUCT"
        v = sanity_violations(sql, {"columns": ["PRODUCT", "c"], "rows": []})
        self.assertTrue(v and "0 rows" in v[0])

    def test_all_null_value_column_flagged(self):
        v = sanity_violations(
            "SELECT product, avg_value FROM x",
            {"columns": ["product", "avg_value"], "rows": [["A", None], ["B", None]]})
        self.assertTrue(v and "entirely NULL" in v[0])

    def test_healthy_result_ok(self):
        v = sanity_violations(
            "SELECT product, avg_value FROM x",
            {"columns": ["product", "avg_value"], "rows": [["A", 1.2], ["B", 3.4]]})
        self.assertEqual(v, [])

    def test_is_aggregate(self):
        self.assertTrue(is_aggregate("SELECT COUNT(*) FROM t"))
        self.assertFalse(is_aggregate("SELECT a FROM t WHERE b=1"))


class DeadlineTests(SimpleTestCase):
    def test_fast_query_returns_payload(self):
        payload = {"columns": [], "rows": [], "row_count": 0, "truncated": False, "sql": "x"}
        with mock.patch.object(texec.executor, "execute_raw_sql", return_value=payload):
            out = texec.execute_with_deadline(object(), "SELECT 1", seconds=5)
        self.assertEqual(out["row_count"], 0)

    def test_slow_query_trips_deadline(self):
        def _slow(*a, **k):
            time.sleep(2)
            return {}
        with mock.patch.object(texec.executor, "execute_raw_sql", side_effect=_slow), \
                mock.patch.object(texec, "get_engine",
                                  return_value=mock.Mock(dispose=mock.Mock())):
            with self.assertRaises(QueryError):
                texec.execute_with_deadline(object(), "SELECT 1", seconds=1)

    def test_underlying_error_propagates(self):
        with mock.patch.object(texec.executor, "execute_raw_sql",
                               side_effect=QueryError("boom")):
            with self.assertRaises(QueryError):
                texec.execute_with_deadline(object(), "SELECT 1", seconds=5)


class BuildSchemaTests(TestCase):
    def setUp(self):
        from connections.models import DataSource
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "egpc.sqlite")
        eng = create_engine(f"sqlite:///{self.path}")
        pd.DataFrame([{"SAMPLE_NUMBER": 1, "PRODUCT": "DIESEL", "IN_SPEC": "T",
                       "INSTRUMENT": None}]).to_sql("SAMPLE", eng, index=False, if_exists="replace")
        pd.DataFrame([{"SAMPLE_NUMBER": 1, "RESULT_NUMBER": 1, "NUMERIC_ENTRY": "1.0",
                       "IN_SPEC": "T"}]).to_sql("RESULT", eng, index=False, if_exists="replace")
        eng.dispose()
        user = get_user_model().objects.create_user(username="schema_t", password="x")
        self.ds = DataSource.objects.create(
            name="egpc", source_type="sqlite", database_name=self.path,
            options={"path": self.path}, owner=user)

    def test_build_schema_exposes_tables_and_do_not_group(self):
        ctx = build_schema(self.ds, question="off-spec by product")
        self.assertIn("SAMPLE", ctx.table_names())
        self.assertIn("RESULT", ctx.table_names())
        sample_cols = {c["name"] for c in ctx.tables["SAMPLE"]}
        self.assertIn("PRODUCT", sample_cols)
        self.assertIn("INSTRUMENT", ctx.do_not_group)
        rendered = ctx.render()
        self.assertIn("DATA NOTES", rendered)
        self.assertIn("SAMPLE", rendered)
