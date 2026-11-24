"""Date, local-time and scope macros (P1): rendered per dialect, and EXECUTED on SQLite.

String checks alone would pass a week bucket that lands on Sunday or a local time
shifted the wrong way; the SQLite cases run the rendered SQL and assert the dates.
"""
import sqlite3
from types import SimpleNamespace

from django.test import SimpleTestCase

from semantics import metric_sql

SQLITE = SimpleNamespace(source_type="sqlite", schema_name="")
MSSQL = SimpleNamespace(source_type="mssql", schema_name="rpt")
POSTGRES = SimpleNamespace(source_type="postgres", schema_name="")


def run_sqlite(expr_sql: str, **values):
    """Evaluate one rendered expression on SQLite with named values bound as columns."""
    con = sqlite3.connect(":memory:")
    cols = ", ".join(f":{k} AS {k}" for k in values) or "1 AS one"
    row = con.execute(f"SELECT {expr_sql} FROM (SELECT {cols})", values).fetchone()
    con.close()
    return row[0]


class BucketTests(SimpleTestCase):
    def render(self, sql, **kw):
        return metric_sql.render(sql, SQLITE, **kw)

    def test_week_starts_on_monday_for_every_weekday(self):
        expr = self.render("{{week:d}}")
        # 2026-11-23 is a Monday.
        for day, monday in (("2026-11-23", "2026-11-23"), ("2026-11-25", "2026-11-23"),
                            ("2026-11-29", "2026-11-23"), ("2026-11-30", "2026-11-30")):
            self.assertEqual(run_sqlite(expr, d=day), monday, day)

    def test_month_and_day_buckets(self):
        self.assertEqual(run_sqlite(self.render("{{month:d}}"), d="2026-11-24 13:05:00"),
                         "2026-11-01")
        self.assertEqual(run_sqlite(self.render("{{day:d}}"), d="2026-11-24 13:05:00"),
                         "2026-11-24")

    def test_days_between_counts_calendar_days(self):
        expr = self.render("{{days_between:a, b}}")
        self.assertEqual(run_sqlite(expr, a="2026-11-01 23:59:00", b="2026-11-02 00:01:00"), 1)
        self.assertEqual(run_sqlite(expr, a="2026-11-24", b="2026-11-24"), 0)


class LocalTimeTests(SimpleTestCase):
    def test_fixed_offset_zone_shifts_utc_to_local(self):
        self.assertEqual(metric_sql.fixed_offset_minutes("Asia/Riyadh"), 180)
        expr = metric_sql.render("{{local:ts}}", SQLITE, timezone="Asia/Riyadh")
        self.assertEqual(run_sqlite(expr, ts="2026-11-24 21:30:00"), "2026-11-25 00:30:00")

    def test_macros_compose_month_of_local_time(self):
        """22:30 UTC on 31 October is already November in Riyadh: the KPI "this month"
        must count it in November."""
        expr = metric_sql.render("{{month:{{local:ts}}}}", SQLITE, timezone="Asia/Riyadh")
        self.assertEqual(run_sqlite(expr, ts="2026-10-31 22:30:00"), "2026-11-01")

    def test_two_argument_macros_accept_rendered_arguments(self):
        expr = metric_sql.render("{{days_between:{{local:a}}, {{local:b}}}}", SQLITE,
                                 timezone="Asia/Riyadh")
        # 20:59 and 21:01 UTC straddle Riyadh midnight: one local calendar day apart.
        self.assertEqual(run_sqlite(expr, a="2026-11-24 20:59:00", b="2026-11-24 21:01:00"), 1)

    def test_without_a_timezone_the_macro_is_left_to_fail_loudly(self):
        self.assertEqual(metric_sql.render("{{local:ts}}", SQLITE), "{{local:ts}}")

    def test_dst_zone_on_a_dialect_that_cannot_convert_is_left_intact(self):
        self.assertIsNone(metric_sql.fixed_offset_minutes("Europe/London"))
        self.assertEqual(metric_sql.render("{{local:ts}}", SQLITE, timezone="Europe/London"),
                         "{{local:ts}}")

    def test_mssql_fixed_offset_uses_switchoffset(self):
        sql = metric_sql.render("{{local:created_at}}", MSSQL, timezone="Asia/Riyadh")
        self.assertEqual(sql, "CAST(SWITCHOFFSET(CAST(created_at AS datetimeoffset), "
                              "'+03:00') AS datetime2)")

    def test_mssql_dst_zone_needs_the_windows_zone_name(self):
        self.assertEqual(metric_sql.render("{{local:t}}", MSSQL, timezone="Europe/London"),
                         "{{local:t}}")
        sql = metric_sql.render("{{local:t}}", MSSQL, timezone="Europe/London",
                                timezone_mssql="GMT Standard Time")
        self.assertIn("AT TIME ZONE 'GMT Standard Time'", sql)

    def test_postgres_dst_zone_converts_with_the_iana_name(self):
        sql = metric_sql.render("{{local:t}}", POSTGRES, timezone="Europe/London")
        self.assertIn("AT TIME ZONE 'Europe/London'", sql)


class ScopeTests(SimpleTestCase):
    def test_all_scope_caller_gets_a_tautology(self):
        self.assertEqual(metric_sql.render("WHERE {{scope:d.project_key}}", MSSQL),
                         "WHERE (1 = 1)")

    def test_scoped_caller_gets_only_their_keys(self):
        self.assertEqual(metric_sql.render("{{scope:project_key}}", MSSQL, scope=["ab12", "cd34"]),
                         "(project_key IN ('ab12', 'cd34'))")
        self.assertEqual(metric_sql.render("{{scope:project_key}}", MSSQL, scope=[]), "(1 = 0)")

    def test_scope_keys_cannot_inject_sql(self):
        with self.assertRaises(ValueError):
            metric_sql.render("{{scope:project_key}}", MSSQL, scope=["x') OR 1=1 --"])


class IntrospectionHelperTests(SimpleTestCase):
    def test_tables_read_and_unknown_macros_see_nested_macros(self):
        sql = ("SELECT COUNT(*) FROM {{table:eng_document_register}} d "
               "JOIN {{table:dim_project}} p ON p.project_key = d.project_key "
               "WHERE {{month:{{local:d.created_at}}}} = {{month:{{lcoal:x}}}}")
        self.assertEqual(metric_sql.tables_read(sql), ["dim_project", "eng_document_register"])
        self.assertEqual(metric_sql.unknown_macros(sql), ["lcoal"])
        self.assertTrue(metric_sql.uses_macro(sql, "local"))
