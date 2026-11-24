"""Databricks and Snowflake as data sources.

DIS does not compete with a customer's lakehouse; it sits on top of one. An enterprise
that has already built a Databricks or Snowflake platform will not rip it out — and it
does not need to. Registering their warehouse as a DataSource makes DIS the layer that
finally gets the lakehouse to *answer a question*.

Everything downstream is dialect-portable and therefore already works against it: the
certified metrics, the Lab Health tiles, the SPC engine, the COA writer. These tests
prove the SQL those features emit is valid for the new dialects — they cannot prove the
connection itself, which needs a real warehouse and an account.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from connections.constants import SOURCE_TYPE_CATALOG, SourceType
from connections.engine import ConnectionConfigError, build_url
from connections.models import DataSource
from semantics import metric_sql
from semantics.spec import load_yaml
from texttosql.sql_tools import dialect_label, inject_row_limit

from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / "semantics" / "specs" / "egpc_dev.yaml"


class _DS:
    """Minimal stand-in — the renderer only reads source_type/schema_name."""

    def __init__(self, source_type, schema_name=""):
        self.source_type = source_type
        self.schema_name = schema_name


class CatalogTests(SimpleTestCase):
    def test_the_lakehouse_types_are_queryable_databases(self):
        self.assertIn(SourceType.DATABRICKS, SourceType.DATABASE_TYPES)
        self.assertIn(SourceType.SNOWFLAKE, SourceType.DATABASE_TYPES)
        self.assertEqual(SourceType.LAKEHOUSE_TYPES,
                         {SourceType.DATABRICKS, SourceType.SNOWFLAKE})

    def test_the_connection_form_knows_what_to_ask_for(self):
        """The frontend renders its form from this catalog, so a missing field is a
        connection the user cannot make."""
        by_value = {e["value"]: e for e in SOURCE_TYPE_CATALOG}

        dbx = by_value[SourceType.DATABRICKS]
        self.assertIn("http_path", dbx["fields"])     # the bit everyone forgets
        self.assertIn("password", dbx["fields"])      # the PAT
        self.assertEqual(dbx["category"], "lakehouse")

        snow = by_value[SourceType.SNOWFLAKE]
        self.assertIn("warehouse", snow["fields"])
        self.assertIn("role", snow["fields"])


class UrlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="dw", password="x")

    def _ds(self, **kw):
        defaults = dict(name="wh", owner=self.user)
        defaults.update(kw)
        return DataSource.objects.create(**defaults)

    def test_databricks_url(self):
        ds = self._ds(source_type=SourceType.DATABRICKS,
                      host="adb-123.4.azuredatabricks.net",
                      database_name="lims_catalog", schema_name="gold",
                      options={"http_path": "/sql/1.0/warehouses/abc123"})
        ds.password = "dapi-secret"

        url = build_url(ds)
        self.assertEqual(url.drivername, "databricks")
        # The connector's convention: the username is the literal string "token".
        self.assertEqual(url.username, "token")
        self.assertEqual(url.password, "dapi-secret")
        self.assertEqual(url.query["http_path"], "/sql/1.0/warehouses/abc123")
        self.assertEqual(url.query["catalog"], "lims_catalog")
        self.assertEqual(url.query["schema"], "gold")

    def test_databricks_without_an_http_path_says_where_to_find_it(self):
        ds = self._ds(source_type=SourceType.DATABRICKS, host="x.databricks.net")
        ds.password = "tok"
        with self.assertRaises(ConnectionConfigError) as ctx:
            build_url(ds)
        self.assertIn("HTTP path", str(ctx.exception))
        self.assertIn("Connection details", str(ctx.exception))

    def test_databricks_without_a_token_is_refused(self):
        ds = self._ds(source_type=SourceType.DATABRICKS, host="x.databricks.net",
                      options={"http_path": "/sql/1.0/warehouses/a"})
        with self.assertRaises(ConnectionConfigError) as ctx:
            build_url(ds)
        self.assertIn("token", str(ctx.exception))

    def test_snowflake_url_puts_database_and_schema_in_the_path(self):
        ds = self._ds(source_type=SourceType.SNOWFLAKE, host="myorg-prod1",
                      username="dis_reader", database_name="LIMS",
                      schema_name="PUBLIC",
                      options={"warehouse": "COMPUTE_WH", "role": "DIS_READONLY"})
        ds.password = "pw"

        url = build_url(ds)
        self.assertEqual(url.drivername, "snowflake")
        self.assertEqual(url.host, "myorg-prod1")
        self.assertEqual(url.database, "LIMS/PUBLIC")   # path, not query params
        self.assertEqual(url.query["warehouse"], "COMPUTE_WH")
        self.assertEqual(url.query["role"], "DIS_READONLY")

    def test_snowflake_needs_the_account_identifier_not_a_hostname(self):
        ds = self._ds(source_type=SourceType.SNOWFLAKE, username="u")
        ds.password = "p"
        with self.assertRaises(ConnectionConfigError) as ctx:
            build_url(ds)
        self.assertIn("account identifier", str(ctx.exception))


class DialectTests(SimpleTestCase):
    def test_both_cap_rows_with_a_trailing_LIMIT(self):
        for st in ("databricks", "snowflake"):
            out = inject_row_limit("SELECT * FROM SAMPLE", st, 50)
            self.assertIn("LIMIT 50", out)
            # …and don't double-cap an already-bounded query.
            self.assertEqual(inject_row_limit(out, st, 50).count("LIMIT"), 1)

    def test_the_generator_is_told_the_right_idioms(self):
        self.assertIn("try_cast", dialect_label("databricks"))
        self.assertIn("TRY_TO_DOUBLE", dialect_label("snowflake"))
        # Snowflake's UPPER-folding is the one thing that will bite a schema author.
        self.assertIn("UPPER", dialect_label("snowflake"))

    def test_censored_values_are_NULL_on_the_lakehouse_too(self):
        """'<0.05' read as 0.0 is how an off-spec batch passes. try_cast and
        TRY_TO_DOUBLE both yield NULL rather than raising or coercing."""
        self.assertIn("try_cast", metric_sql.safe_number("r.V", "databricks"))
        self.assertIn("TRY_TO_DOUBLE", metric_sql.safe_number("r.V", "snowflake"))

    def test_date_macros(self):
        self.assertIn("date_sub(current_date()", metric_sql.days_ago(30, "databricks"))
        self.assertIn("DATEADD(day, -30", metric_sql.days_ago(30, "snowflake"))


class TurnaroundIsTheSameNumberEverywhereTests(SimpleTestCase):
    """A bug this connector work surfaced.

    ``DATEDIFF(hour, …)`` on MSSQL and ``TIMESTAMPDIFF(HOUR, …)`` on MySQL return
    WHOLE hours: a sample logged 08:00 and completed 09:59 counts as 1 hour, not 1.98.
    Postgres and SQLite return the fraction. So the same turnaround tile answered
    differently depending on which datasource it ran against — a lab moving from the
    LIMS source to the replica would have watched its mean TAT change for no reason.
    Every dialect now computes in SECONDS and divides.
    """

    def test_no_dialect_truncates_to_whole_hours(self):
        for d in ("mssql", "mysql", "snowflake"):
            sql = metric_sql.hours_between("a", "b", d)
            self.assertIn("3600", sql, d)
            self.assertNotIn("DATEDIFF(hour", sql)
            self.assertNotIn("TIMESTAMPDIFF(HOUR", sql)

    def test_every_dialect_yields_fractional_hours(self):
        for d in ("mssql", "postgres", "sqlite", "mysql", "oracle",
                  "databricks", "snowflake"):
            sql = metric_sql.hours_between("s.LOGIN_DATE", "s.DATE_COMPLETED", d)
            self.assertTrue(
                "3600" in sql or "24.0" in sql or "* 24" in sql,
                f"{d} does not produce fractional hours: {sql}")


class TheWholeSemanticLayerRendersOnALakehouseTests(SimpleTestCase):
    """The point of the connector: everything downstream already works.

    The certified metrics and the Lab Health tiles are portable, so pointing DIS at a
    lakehouse needs no new SQL — but "should work" is not "does work", so render every
    shipped definition for both dialects and assert no macro survives.
    """

    def setUp(self):
        spec = load_yaml(SPEC)
        self.metrics = spec["metrics"]
        self.health = spec["health"]

    def test_every_certified_metric_renders(self):
        for dialect in ("databricks", "snowflake"):
            for m in self.metrics:
                out = metric_sql.render(m["sql"], _DS(dialect))
                self.assertNotIn("{{", out, f"{m['key']} on {dialect}")

    def test_every_health_tile_renders(self):
        for dialect in ("databricks", "snowflake"):
            for t in self.health:
                out = metric_sql.render(t["sql"], _DS(dialect))
                self.assertNotIn("{{", out, f"{t['key']} on {dialect}")

    def test_a_metric_renders_with_the_lakehouse_idioms_not_T_SQL(self):
        m = next(m for m in self.metrics if m["key"] == "sulphur_by_product")
        dbx = metric_sql.render(m["sql"], _DS("databricks"))
        self.assertIn("try_cast", dbx)
        self.assertNotIn("TRY_CONVERT", dbx)
        self.assertNotIn("dbo.", dbx)
