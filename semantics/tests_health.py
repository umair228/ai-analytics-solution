"""Lab Health tiles — the first screen a lab manager sees.

It used to show counts of app objects (connections, datasets, dashboards): true,
and of no interest to anyone who runs a laboratory. These tiles show the lab, and
they go through the same certified-SQL machinery as any other answer — the home
page must not be the one screen where numbers appear without provenance.

As in tests_portability, the load-bearing test EXECUTES every shipped tile against
a real replica-shaped database. Rendering plausible SQL is not the bar.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from semantics import metric_sql
from semantics.spec import load_yaml, validate_health

SPEC = Path(__file__).resolve().parent / "specs" / "egpc_dev.yaml"


class _DS:
    def __init__(self, source_type, schema_name=""):
        self.source_type = source_type
        self.schema_name = schema_name


class DateMacroTests(SimpleTestCase):
    def test_days_ago_per_dialect(self):
        self.assertIn("DATEADD(day, -30", metric_sql.days_ago(30, "mssql"))
        self.assertIn("INTERVAL '30 days'", metric_sql.days_ago(30, "postgres"))
        self.assertIn("-30 days", metric_sql.days_ago(30, "sqlite"))

    def test_hours_between_casts_to_numeric_on_postgres(self):
        """Postgres has no ROUND(double precision, int), and every TAT tile rounds.
        Returning a double here would make each one fail on the replica — the same
        trap safe_number had."""
        sql = metric_sql.hours_between("a", "b", "postgres")
        self.assertIn("AS numeric", sql)
        self.assertIn("EXTRACT(EPOCH", sql)

    def test_hours_between_is_FRACTIONAL_on_every_dialect(self):
        """It used to be ``DATEDIFF(hour, …)`` on MSSQL, which returns WHOLE hours: a
        sample logged 08:00 and completed 09:59 counted as 1 hour, not 1.98. Postgres
        and SQLite return the fraction — so the same turnaround tile answered
        differently depending on which datasource it ran against, and a lab moving from
        the LIMS source to the replica would have watched its mean TAT change for no
        reason at all. Every dialect now computes in seconds and divides."""
        mssql = metric_sql.hours_between("a", "b", "mssql")
        self.assertIn("DATEDIFF(second", mssql)
        self.assertIn("3600", mssql)
        self.assertNotIn("DATEDIFF(hour", mssql)

        self.assertIn("julianday", metric_sql.hours_between("a", "b", "sqlite"))


class ShippedHealthSpecTests(SimpleTestCase):
    def setUp(self):
        self.tiles = load_yaml(SPEC)["health"]

    def test_spec_passes_static_validation(self):
        self.assertEqual(validate_health({"health": self.tiles}), [])

    def test_tiles_render_for_every_dialect(self):
        for dialect in ("mssql", "postgres", "sqlite"):
            for t in self.tiles:
                out = metric_sql.render(t["sql"], _DS(dialect))
                self.assertNotIn("{{", out, f"{t['key']} on {dialect} left a macro")

    def test_graded_tiles_declare_a_direction(self):
        # A target with no direction is ungradeable: we would not know which side of
        # the line is good, so the tile would be coloured arbitrarily.
        for t in self.tiles:
            if t.get("target") is not None:
                self.assertIn(t.get("direction"), ("lower_better", "higher_better"), t["key"])

    def test_rate_tiles_guard_against_divide_by_zero(self):
        """An empty 30-day window divides by zero — which Postgres raises on, so the
        tile would not read 0, it would fail."""
        rate = next(t for t in self.tiles if t["key"] == "off_spec_rate_30d")
        self.assertIn("NULLIF(COUNT(*), 0)", rate["sql"])


class ValidateHealthTests(SimpleTestCase):
    def test_target_without_direction_is_rejected(self):
        errs = validate_health({"health": [
            {"key": "k", "label": "L", "sql": "SELECT 1", "target": 5}]})
        self.assertTrue(any("direction" in e for e in errs))

    def test_unknown_unit_is_rejected(self):
        errs = validate_health({"health": [
            {"key": "k", "label": "L", "sql": "SELECT 1", "unit": "furlongs"}]})
        self.assertTrue(any("unknown unit" in e for e in errs))

    def test_group_board_and_kpi_id_must_be_short_text(self):
        errs = validate_health({"health": [
            {"key": "k", "label": "L", "sql": "SELECT 1", "group": ["Engineering"],
             "board": "B" * 201, "kpi_id": "ENG-01"}]})
        self.assertTrue(any("group: must be text" in e for e in errs))
        self.assertTrue(any("board: must be text" in e for e in errs))
        self.assertFalse(any("kpi_id" in e for e in errs))

    def test_unknown_macro_is_rejected(self):
        errs = validate_health({"health": [
            {"key": "k", "label": "L", "sql": "SELECT {{nmu:x}}"}]})
        self.assertTrue(any("unknown macro" in e for e in errs))


class RealSqliteHealthTests(TestCase):
    """Execute every shipped tile against a real replica-shaped database."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "replica.sqlite")
        con = sqlite3.connect(cls.db)
        con.executescript("""
            CREATE TABLE sample (
              sample_number INTEGER, text_id TEXT, product TEXT, sample_type TEXT,
              status TEXT, in_spec TEXT, login_date TEXT, date_completed TEXT);
            CREATE TABLE result (
              sample_number INTEGER, result_number INTEGER, analysis TEXT, name TEXT,
              numeric_entry TEXT, units TEXT, in_spec TEXT, entered_by TEXT);
            -- 2 recent samples: one completed off-spec, one still in progress.
            INSERT INTO sample VALUES
              (1,'S1','Diesel','SCHEDULED','C','F',date('now','-2 days'),date('now','-1 days')),
              (2,'S2','Diesel','UNSCHEDULED','I','T',date('now','-1 days'),NULL),
              (3,'S3','Gasoline','SCHEDULED','X','T',date('now','-3 days'),NULL),
              -- and one OLD sample, outside every 30-day window
              (4,'S4','Diesel','SCHEDULED','C','T',date('now','-400 days'),date('now','-399 days'));
            INSERT INTO result VALUES
              (1,1,'SULFUR_TOTAL','Sulfur','12.5','ppm','F','a.hassan'),
              (1,2,'CETANE_INDEX','Cetane','51.2','','T','a.hassan'),
              (4,1,'OCTANE_NO','Octane','95.4','','T','m.said');
        """)
        con.commit()
        con.close()
        cls.tiles = load_yaml(SPEC)["health"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def _values(self):
        con = sqlite3.connect(self.db)
        try:
            out = {}
            for t in self.tiles:
                sql = metric_sql.render(t["sql"], _DS("sqlite"))
                rows = con.execute(sql).fetchall()
                self.assertEqual(len(rows), 1, f"{t['key']} must return exactly one row")
                self.assertEqual(len(rows[0]), 1, f"{t['key']} must return exactly one column")
                out[t["key"]] = rows[0][0]
            return out
        finally:
            con.close()

    def test_every_tile_executes_and_returns_a_scalar(self):
        self._values()   # raises if any tile is invalid or non-scalar

    def test_tiles_compute_the_right_numbers(self):
        v = self._values()
        # 3 samples within 30 days (the 400-day-old one is excluded).
        self.assertEqual(v["samples_30d"], 3)
        # Backlog = status U/P/I -> only S2.
        self.assertEqual(v["backlog"], 1)
        # Cancelled/rejected in 30d -> only S3 (status X).
        self.assertEqual(v["cancelled_30d"], 1)
        # Unscheduled in 30d -> only S2.
        self.assertEqual(v["unscheduled_30d"], 1)
        # Off-spec rate over results whose SAMPLE is inside the window: sample 1 has
        # 2 results, one off-spec -> 50%. Sample 4's result is outside the window.
        self.assertEqual(v["off_spec_rate_30d"], 50.0)
        # TAT: only S1 completed inside the window; login->complete is 1 day = 24h.
        self.assertEqual(v["tat_hours_30d"], 24.0)

    def test_an_empty_window_yields_null_not_a_divide_by_zero(self):
        rate = next(t for t in self.tiles if t["key"] == "off_spec_rate_30d")
        sql = metric_sql.render(rate["sql"], _DS("sqlite"))
        con = sqlite3.connect(":memory:")
        try:
            con.executescript("""
                CREATE TABLE sample (sample_number INTEGER, login_date TEXT);
                CREATE TABLE result (sample_number INTEGER, in_spec TEXT);
            """)
            self.assertIsNone(con.execute(sql).fetchone()[0])
        finally:
            con.close()


class LabHealthApiTests(TestCase):
    def setUp(self):
        from connections.models import DataSource
        from semantics import layer as layer_mod
        from semantics.spec import apply_spec

        cache.clear()
        layer_mod.invalidate()

        self.tmp = tempfile.TemporaryDirectory()
        db = str(Path(self.tmp.name) / "lims.sqlite")
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE sample (sample_number INTEGER, product TEXT, sample_type TEXT,
              status TEXT, in_spec TEXT, login_date TEXT, date_completed TEXT);
            CREATE TABLE result (sample_number INTEGER, in_spec TEXT);
            INSERT INTO sample VALUES
              (1,'Diesel','SCHEDULED','C','F',date('now','-2 days'),date('now','-1 days')),
              (2,'Diesel','UNSCHEDULED','I','T',date('now','-1 days'),NULL);
            INSERT INTO result VALUES (1,'F'), (1,'T');
        """)
        con.commit(); con.close()

        self.user = get_user_model().objects.create_user(
            username="mgr", password="x", role="lab_manager")
        self.analyst = get_user_model().objects.create_user(
            username="an", password="x", role="analyst")
        self.ds = DataSource.objects.create(
            name="EGPC", source_type="sqlite", database_name=db,
            options={"path": db}, owner=self.user)
        self.ds.shared_with.add(self.analyst)
        apply_spec(self.ds, load_yaml(SPEC), verify=False)

    def tearDown(self):
        self.tmp.cleanup()
        cache.clear()

    def test_lab_manager_sees_the_lab_not_the_app(self):
        from semantics.health import lab_health

        out = lab_health(self.user)
        self.assertTrue(out["configured"])
        tiles = {t["key"]: t for t in out["tiles"]}

        self.assertEqual(tiles["samples_30d"]["value"], 2)
        self.assertEqual(tiles["backlog"]["value"], 1)
        self.assertEqual(tiles["off_spec_rate_30d"]["value"], 50.0)
        # 50% against a 2% target, lower-is-better -> bad, not merely "a number".
        self.assertEqual(tiles["off_spec_rate_30d"]["grade"], "bad")
        # Freshness rides along, exactly as it does on an answer.
        self.assertEqual(out["freshness"], "live")

    def test_tiles_are_scoped_to_the_role(self):
        from semantics.health import lab_health

        mgr = {t["key"] for t in lab_health(self.user)["tiles"]}
        cache.clear()
        analyst = {t["key"] for t in lab_health(self.analyst)["tiles"]}

        self.assertIn("cancelled_30d", mgr)        # intake quality: a manager's problem
        self.assertNotIn("cancelled_30d", analyst)  # not an analyst's
        self.assertIn("backlog", analyst)           # but the backlog certainly is

    def test_tiles_carry_their_area_board_and_code(self):
        """The management home groups tiles by area and opens the board behind them."""
        from semantics import layer as layer_mod
        from semantics.health import lab_health
        from semantics.models import HealthTile

        HealthTile.objects.filter(datasource=self.ds, key="backlog").update(
            group="Laboratory", board="Lab Overview", kpi_id="LAB-02")
        cache.clear()
        layer_mod.invalidate()

        tiles = {t["key"]: t for t in lab_health(self.user)["tiles"]}
        self.assertEqual((tiles["backlog"]["group"], tiles["backlog"]["board"],
                          tiles["backlog"]["kpi_id"]), ("Laboratory", "Lab Overview", "LAB-02"))
        self.assertEqual(tiles["samples_30d"]["group"], "")      # optional: older specs

    def test_a_reloaded_spec_shows_at_once_despite_the_cache(self):
        """Tiles are cached for hours on a snapshot; a reload must not wait that out."""
        from semantics.health import lab_health
        from semantics.spec import apply_spec

        self.ds.options = {**self.ds.options, "restore_id": "night-1"}   # long cache
        self.ds.save(update_fields=["options"])
        self.assertEqual(lab_health(self.user)["tiles"][0]["group"], "")
        spec = load_yaml(SPEC)
        spec["health"][0]["group"] = "Intake"
        import time
        time.sleep(0.01)                     # a later updated_at
        apply_spec(self.ds, spec, verify=False)
        self.assertEqual(lab_health(self.user)["tiles"][0]["group"], "Intake")

    def test_a_healthy_number_grades_good(self):
        from semantics.health import lab_health
        from semantics.models import HealthTile

        HealthTile.objects.filter(datasource=self.ds, key="off_spec_rate_30d").update(target=80.0)
        cache.clear()
        from semantics import layer as layer_mod
        layer_mod.invalidate()

        tiles = {t["key"]: t for t in lab_health(self.user)["tiles"]}
        self.assertEqual(tiles["off_spec_rate_30d"]["grade"], "good")   # 50% <= 80%

    def test_a_broken_tile_reads_as_broken_not_as_zero(self):
        """A lab manager seeing '0 off-spec' because the query failed is worse off
        than one seeing no number at all."""
        from semantics.health import lab_health
        from semantics.models import HealthTile
        from semantics import layer as layer_mod

        HealthTile.objects.filter(datasource=self.ds, key="backlog").update(
            sql="SELECT COUNT(*) FROM {{table:NO_SUCH_TABLE}}")
        layer_mod.invalidate()
        cache.clear()

        tiles = {t["key"]: t for t in lab_health(self.user)["tiles"]}
        self.assertIsNone(tiles["backlog"]["value"])
        self.assertNotEqual(tiles["backlog"]["error"], "")
        # …and one bad tile must not blank the rest of the page.
        self.assertEqual(tiles["samples_30d"]["value"], 2)

    def test_no_semantic_layer_degrades_honestly(self):
        from connections.models import DataSource
        from semantics.health import lab_health

        bare_user = get_user_model().objects.create_user(username="bare", password="x")
        DataSource.objects.create(name="RAW", source_type="sqlite",
                                  database_name="/tmp/none.sqlite",
                                  options={"path": "/tmp/none.sqlite"}, owner=bare_user)
        out = lab_health(bare_user)
        self.assertFalse(out["configured"])
        self.assertEqual(out["reason"], "no_semantic_layer")
