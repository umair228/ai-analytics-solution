"""Certified metrics must run on the LIMS source AND the replica.

Before this, they didn't. Every metric was hardcoded T-SQL (`TOP 50`,
`TRY_CONVERT`, `dbo.`), so against a Postgres/SQLite replica each one raised,
`texttosql.runner` caught the QueryError and fell through to the LLM. The
certified fast path and the replica silently excluded each other, and the only
symptom was that the "certified" badge stopped appearing.

The strongest test here is `RealSqliteExecutionTests`: it builds an actual
replica-shaped SQLite database and EXECUTES all 12 shipped metrics against it.
A metric that renders into plausible-looking but invalid SQL fails here.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from semantics import metric_sql
from semantics.spec import SpecError, load_yaml, validate_metrics

SPEC = Path(__file__).resolve().parent / "specs" / "egpc_dev.yaml"


class _DS:
    """A stand-in DataSource — the renderer only reads source_type/schema_name."""

    def __init__(self, source_type, schema_name=""):
        self.source_type = source_type
        self.schema_name = schema_name


class SafeNumberTests(SimpleTestCase):
    def test_mssql_uses_try_convert(self):
        self.assertEqual(metric_sql.safe_number("r.V", "mssql"), "TRY_CONVERT(float, r.V)")

    def test_postgres_casts_to_numeric_not_double(self):
        """Postgres has no ROUND(double precision, int). The metrics round, so a
        double cast makes every ROUND(AVG(...), 4) metric fail on the replica with
        'function round(double precision, integer) does not exist'."""
        sql = metric_sql.safe_number("r.V", "postgres")
        self.assertIn("CAST(r.V AS numeric)", sql)
        self.assertNotIn("double precision", sql)

    def test_every_dialect_guards_non_numeric_text(self):
        # LabWare entries like "<0.05" or "ND" must become NULL, never 0 — a
        # censored result silently read as zero is how an off-spec batch passes.
        for dialect in ("postgres", "sqlite", "mysql"):
            self.assertIn("CASE", metric_sql.safe_number("r.V", dialect))


class RenderTests(SimpleTestCase):
    def test_table_macro_qualifies_mssql_with_dbo(self):
        out = metric_sql.render("SELECT * FROM {{table:RESULT}}", _DS("mssql"))
        self.assertEqual(out, "SELECT * FROM dbo.RESULT")

    def test_table_macro_is_bare_on_the_replica(self):
        out = metric_sql.render("SELECT * FROM {{table:RESULT}}", _DS("postgres"))
        self.assertEqual(out, "SELECT * FROM RESULT")

    def test_explicit_schema_wins(self):
        out = metric_sql.render("SELECT * FROM {{table:RESULT}}", _DS("postgres", "egpc"))
        self.assertEqual(out, "SELECT * FROM egpc.RESULT")

    def test_identifiers_are_left_unquoted(self):
        """Unquoted is what makes one SQL work on three dialects: MSSQL/SQLite are
        case-insensitive, and Postgres folds to lower — which matches the lower-case
        replica (replication.loader._canon)."""
        out = metric_sql.render("SELECT s.PRODUCT FROM {{table:SAMPLE}} s", _DS("postgres"))
        self.assertNotIn('"', out)

    def test_unknown_macro_is_reported(self):
        self.assertEqual(metric_sql.unknown_macros("SELECT {{nmu:x}}, {{num:y}}"), ["nmu"])


class ShippedSpecIsPortableTests(SimpleTestCase):
    """Guard the actual egpc_dev.yaml that production loads."""

    def setUp(self):
        self.metrics = load_yaml(SPEC)["metrics"]

    def test_spec_passes_static_validation(self):
        self.assertEqual(validate_metrics({"metrics": self.metrics}), [])

    def test_no_metric_contains_t_sql(self):
        for m in self.metrics:
            sql = m["sql"].upper()
            self.assertNotIn("TRY_CONVERT", sql, m["key"])   # use {{num:…}}
            self.assertNotIn("DBO.", sql, m["key"])          # use {{table:…}}
            self.assertNotIn("SELECT TOP", sql, m["key"])    # use limit:

    def test_metrics_render_for_every_supported_dialect(self):
        for dialect in ("mssql", "postgres", "sqlite"):
            for m in self.metrics:
                out = metric_sql.render(m["sql"], _DS(dialect))
                self.assertNotIn("{{", out, f"{m['key']} on {dialect} left a macro")

    def test_semantic_limits_survive_as_row_limit(self):
        by_key = {m["key"]: m for m in self.metrics}
        # "top 5 tests" means five — that meaning lived in `TOP 5` and must not be
        # lost when the T-SQL goes away.
        self.assertEqual(by_key["top5_tests"]["limit"], 5)
        self.assertEqual(by_key["most_used_product"]["limit"], 5)
        self.assertEqual(by_key["sulphur_by_product"]["limit"], 50)
        self.assertEqual(by_key["oos_by_test_count"]["limit"], 30)


class ValidateMetricsTests(SimpleTestCase):
    def test_hardcoded_top_is_rejected(self):
        errs = validate_metrics({"metrics": [
            {"key": "m", "sql": "SELECT TOP 5 A FROM {{table:RESULT}}"}]})
        self.assertTrue(any("TOP n" in e for e in errs))

    def test_unknown_macro_is_rejected(self):
        errs = validate_metrics({"metrics": [
            {"key": "m", "sql": "SELECT {{nmu:x}} FROM {{table:RESULT}}"}]})
        self.assertTrue(any("unknown macro" in e for e in errs))

    def test_duplicate_key_is_rejected(self):
        errs = validate_metrics({"metrics": [
            {"key": "m", "sql": "SELECT 1"}, {"key": "m", "sql": "SELECT 2"}]})
        self.assertTrue(any("more than once" in e for e in errs))


class RealSqliteExecutionTests(TestCase):
    """Execute all 12 shipped metrics against a real replica-shaped database.

    This is the test that would have caught the original bug. Rendering to
    *plausible* SQL is not the bar; the bar is SQL the database accepts.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "replica.sqlite")
        con = sqlite3.connect(cls.db)
        # Lower case, exactly as replication.loader._canon writes it.
        con.executescript("""
            CREATE TABLE sample (
              sample_number INTEGER, text_id TEXT, product TEXT, sample_type TEXT,
              status TEXT, in_spec TEXT, login_date TEXT, date_completed TEXT,
              changed_on TEXT);
            CREATE TABLE result (
              sample_number INTEGER, result_number INTEGER, analysis TEXT, name TEXT,
              numeric_entry TEXT, units TEXT, in_spec TEXT, entered_by TEXT,
              entered_on TEXT, changed_on TEXT);
            INSERT INTO sample VALUES
              (1,'S1','Diesel','SCHEDULED','C','F','2026-01-01','2026-01-02',''),
              (2,'S2','Diesel','UNSCHEDULED','X','T','2026-01-03','2026-01-04',''),
              (3,'S3','Gasoline','SCHEDULED','A','T','2026-01-05','2026-01-06','');
            INSERT INTO result VALUES
              (1,1,'SULFUR_TOTAL','Sulfur','12.5','ppm','F','a.hassan','2026-01-02',''),
              (1,2,'CETANE_INDEX','Cetane','51.2','','T','a.hassan','2026-01-02',''),
              (2,1,'SULFUR_TOTAL','Sulfur','<0.05','ppm','T','m.said','2026-01-04',''),
              (3,1,'OCTANE_NO','Octane','95.4','','T','m.said','2026-01-06','');
        """)
        con.commit()
        con.close()
        cls.metrics = load_yaml(SPEC)["metrics"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def test_every_shipped_metric_executes_on_a_replica(self):
        from texttosql.sql_tools import inject_row_limit

        con = sqlite3.connect(self.db)
        try:
            for m in self.metrics:
                sql = metric_sql.render(m["sql"], _DS("sqlite"))
                sql = inject_row_limit(sql, "sqlite", min(m.get("limit") or 5000, 5000))
                with self.subTest(metric=m["key"]):
                    con.execute(sql).fetchall()   # raises if the SQL is invalid
        finally:
            con.close()

    def test_censored_results_become_null_not_zero(self):
        """LabWare is full of censored/qualitative entries. Read as 0 they drag an
        average down and can turn an off-spec batch into a passing one.

        SQLite makes this a live trap: CAST('<0.05' AS REAL) is 0.0, not NULL.
        """
        expr = metric_sql.safe_number("v", "sqlite")
        con = sqlite3.connect(":memory:")
        try:
            not_a_number = ["<0.05", ">100", "ND", "N/A", "TRACE", "", "  ", "-", "."]
            for raw in not_a_number:
                got = con.execute(
                    f"SELECT {expr} FROM (SELECT ? AS v)", (raw,)).fetchone()[0]
                self.assertIsNone(got, f"{raw!r} must be NULL, got {got!r}")

            for raw, want in [("12.5", 12.5), ("-3", -3.0), ("0", 0.0), ("0.0", 0.0)]:
                got = con.execute(
                    f"SELECT {expr} FROM (SELECT ? AS v)", (raw,)).fetchone()[0]
                self.assertEqual(got, want, f"{raw!r} must parse to {want}")
        finally:
            con.close()

    def test_mart_cast_has_the_same_guard(self):
        """The marts feed mart_results.value; the same censored value must not land
        there as 0.0 either. replication.sql_dialect must agree with metric_sql."""
        from replication.sql_dialect import safe_float

        expr = safe_float("v", "sqlite")
        con = sqlite3.connect(":memory:")
        try:
            got = con.execute(f"SELECT {expr} FROM (SELECT '<0.05' AS v)").fetchone()[0]
            self.assertIsNone(got)
        finally:
            con.close()

    def test_top5_metric_actually_caps_at_five(self):
        """The semantic meaning of `TOP 5` must survive the move to `limit:`."""
        from texttosql.sql_tools import inject_row_limit

        m = next(m for m in self.metrics if m["key"] == "top5_tests")
        sql = inject_row_limit(metric_sql.render(m["sql"], _DS("sqlite")), "sqlite",
                               min(m["limit"], 5000))
        self.assertIn("LIMIT 5", sql)

        # …and as TOP 5 on the source, from the same definition.
        mssql = inject_row_limit(metric_sql.render(m["sql"], _DS("mssql")), "mssql",
                                 min(m["limit"], 5000))
        self.assertIn("SELECT TOP 5", mssql)
        self.assertIn("dbo.RESULT", mssql)


class SemanticLayerCacheTests(TestCase):
    def test_layer_is_cached_and_invalidated_on_reload(self):
        from django.contrib.auth import get_user_model
        from connections.models import DataSource
        from semantics import layer as layer_mod
        from semantics.models import CertifiedMetric, SemanticLayer, SemanticTable

        user = get_user_model().objects.create_user(username="sem", password="x")
        ds = DataSource.objects.create(name="DS", source_type="sqlite",
                                       database_name="/tmp/x.sqlite",
                                       options={"path": "/tmp/x.sqlite"}, owner=user)
        SemanticLayer.objects.create(datasource=ds, notes="v1")
        SemanticTable.objects.create(datasource=ds, name="RESULT")
        CertifiedMetric.objects.create(datasource=ds, key="k", name="K",
                                       sql="SELECT 1", row_limit=7)

        layer_mod.invalidate()
        first = layer_mod.get_semantic_layer(ds)
        self.assertIs(layer_mod.get_semantic_layer(ds), first)   # cached, same object
        self.assertEqual(first.metrics[0].row_limit, 7)

        # A reload bumps SemanticLayer.updated_at, which must change the cache key.
        SemanticLayer.objects.filter(datasource=ds).update(notes="v2")
        SemanticLayer.objects.get(datasource=ds).save()
        self.assertIsNot(layer_mod.get_semantic_layer(ds), first)
        self.assertEqual(layer_mod.get_semantic_layer(ds).notes, "v2")
