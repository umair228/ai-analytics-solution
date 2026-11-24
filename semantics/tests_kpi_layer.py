"""The certified KPI layer (R2): scope loader, classification, KPI metadata (P5),
clearance, snapshot freshness, nightly observations and the parity check.

The database-backed cases run a real SQLite "reporting copy" shaped like the PMSv2
rpt views, so the rules are proven by executing the SQL, not by reading it.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import yaml
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from semantics import layer as layer_mod
from semantics.spec import (validate_health, validate_layer, validate_metrics,
                            verify_classification)

ACTIVE_SQL = ("SELECT SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) "
              "FROM {{table:dim_project}} WHERE is_excluded = 0 AND {{scope:project_key}}")


def classified_spec(**over):
    base = {
        "timezone": "Asia/Riyadh",
        "classification": {"required": True},
        "tables": {
            "dim_project": {"scope": "scoped:project"},
            "rfq": {"scope": "all_scope_only"},
            "rfq_value": {"scope": "all_scope_only", "classes": ["bid_confidential"]},
        },
        "metrics": [],
        "health": [],
    }
    base.update(over)
    return base


def all_errors(spec):
    return validate_layer(spec) + validate_metrics(spec) + validate_health(spec)


class ScopeLoaderTests(SimpleTestCase):
    def test_a_metric_on_a_project_scoped_view_needs_the_scope_filter(self):
        bad = classified_spec(metrics=[{"key": "k", "sql": "SELECT COUNT(*) FROM {{table:dim_project}}"}])
        self.assertTrue(any("without a {{scope:…}} filter" in e for e in all_errors(bad)))
        good = classified_spec(metrics=[{"key": "k", "sql": ACTIVE_SQL}])
        self.assertEqual(all_errors(good), [])

    def test_unclassified_and_directly_named_tables_are_refused(self):
        errs = all_errors(classified_spec(metrics=[{"key": "k", "sql": "SELECT 1 FROM {{table:mystery}}"}]))
        self.assertTrue(any("'mystery', which has no `scope:`" in e for e in errs), errs)
        errs = all_errors(classified_spec(metrics=[{"key": "k", "sql": "SELECT COUNT(*) FROM rpt.rfq"}]))
        self.assertTrue(any("names table 'rpt.rfq' directly" in e for e in errs), errs)
        self.assertTrue(any("reads no {{table:…}}" in e for e in errs), errs)

    def test_tiles_follow_the_same_rules(self):
        spec = classified_spec(health=[{"key": "t", "label": "T",
                                        "sql": "SELECT COUNT(*) FROM {{table:dim_project}}"}])
        self.assertTrue(any(e.startswith("health tile 't'") and "scope" in e for e in all_errors(spec)))

    def test_specs_that_do_not_ask_for_classification_load_as_before(self):
        spec = {"metrics": [{"key": "k", "sql": "SELECT COUNT(*) FROM SAMPLE"}]}
        self.assertEqual(all_errors(spec), [])

    def test_local_time_needs_a_timezone_and_vocabularies_are_closed(self):
        errs = all_errors(classified_spec(timezone=None, classification={}, metrics=[
            {"key": "k", "sql": "SELECT {{local:created_at}} FROM {{table:rfq}}"}]))
        self.assertTrue(any("declares no `timezone:`" in e for e in errs), errs)
        errs = validate_layer(classified_spec(timezone="Mars/Olympus", tables={
            "x": {"scope": "everyone", "classes": ["secret"]}}))
        self.assertEqual(len(errs), 3, errs)


class KpiMetadataTests(SimpleTestCase):
    def test_unit_direction_status_and_threshold_keys_are_checked(self):
        errs = validate_metrics({"metrics": [{
            "key": "k", "sql": "SELECT 1", "unit": "parsecs", "direction": "sideways",
            "status": "approved", "thresholds": {"target": 1, "goal": 2}}]})
        for fragment in ("unknown unit 'parsecs'", "unknown direction 'sideways'",
                         "unknown status 'approved'", "thresholds may only set"):
            self.assertTrue(any(fragment in e for e in errs), (fragment, errs))

    def test_bands_need_a_direction_and_the_right_order(self):
        errs = validate_metrics({"metrics": [{"key": "k", "sql": "SELECT 1",
                                              "direction": "lower_better",
                                              "thresholds": {"warn": 30, "critical": 20}}]})
        self.assertTrue(any("warn (30) must come before critical (20)" in e for e in errs), errs)
        errs = validate_metrics({"metrics": [{"key": "k", "sql": "SELECT 1",
                                              "thresholds": {"critical": 20}}]})
        self.assertTrue(any("direction is 'neutral'" in e for e in errs), errs)

    def test_an_observed_metric_must_be_a_single_number(self):
        errs = validate_metrics({"metrics": [{"key": "k", "sql": "SELECT 1", "observe": True, "limit": 5}]})
        self.assertTrue(any("must be a single number" in e for e in errs), errs)

    def test_explicit_bands_grade_the_tile(self):
        from semantics.health import _grade

        tile = SimpleNamespace(direction="lower_better", target=None, warn=20, critical=26)
        self.assertEqual([_grade(v, tile) for v in (5, 20, 25.9, 26, 40)],
                         ["good", "warn", "warn", "bad", "bad"])
        neutral = SimpleNamespace(direction="neutral", target=None, warn=None, critical=26)
        self.assertEqual(_grade(40, neutral), "none")


class KpiLayerOnSqliteTests(TestCase):
    def setUp(self):
        from connections.models import DataSource

        cache.clear()
        layer_mod.invalidate()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "rpt.sqlite")
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE dim_project (project_key TEXT, status TEXT, is_excluded INTEGER);
            INSERT INTO dim_project VALUES ('p1','active',0), ('p2','active',0),
                                           ('p3','closed',0), ('p4','active',1);
            CREATE TABLE rfq_value (rfq_key TEXT, value_sar REAL);
            INSERT INTO rfq_value VALUES ('r1', 100.0);
            CREATE TABLE view_catalog (view_name TEXT, scope_class TEXT, classes TEXT);
            INSERT INTO view_catalog VALUES ('dim_project', 'scoped:project', ''),
                                            ('rfq_value', 'all_scope_only', 'bid_confidential');
            CREATE TABLE exec_summary (kpi_id TEXT, value REAL);
            INSERT INTO exec_summary VALUES ('PRJ-01', 2);
            CREATE TABLE v_data_as_of (data_as_of_riyadh TEXT);
        """)
        con.commit()
        con.close()
        User = get_user_model()
        self.admin = User.objects.create_user(username="adm", password="x", role="admin")
        self.viewer = User.objects.create_user(username="v", password="x", role="viewer")
        self.ds = DataSource.objects.create(
            name="Rpt", source_type="sqlite", database_name=self.db,
            options={"path": self.db, "restore_id": "r-1"}, owner=self.admin, visibility="org")
        self.spec = {
            "timezone": "Asia/Riyadh",
            "classification": {"required": True, "catalog": {
                "table": "view_catalog", "name": "view_name", "scope": "scope_class",
                "classes": "classes"}},
            "parity": {"sql": "SELECT value FROM {{table:exec_summary}} WHERE kpi_id = :kpi_id"},
            "tables": {"dim_project": {"scope": "scoped:project"},
                       "rfq_value": {"scope": "all_scope_only", "classes": ["bid_confidential"]}},
            "metrics": [{"key": "active", "kpi_id": "PRJ-01", "unit": "count", "observe": True,
                         "status": "parity_checked", "owner": "PMO", "sql": ACTIVE_SQL}],
            "health": [{"key": "active", "label": "Active projects", "sql": ACTIVE_SQL},
                       {"key": "bid_value", "label": "Bid value", "unit": "currency",
                        "sql": "SELECT SUM(value_sar) FROM {{table:rfq_value}}"}],
        }
        from semantics.spec import apply_spec

        apply_spec(self.ds, self.spec, verify=False)
        self.spec_file = Path(self.tmp.name) / "rpt.yaml"
        self.spec_file.write_text(yaml.safe_dump(self.spec))

    def tearDown(self):
        self.tmp.cleanup()
        cache.clear()
        layer_mod.invalidate()

    def sql(self, statement):
        con = sqlite3.connect(self.db)
        con.execute(statement)
        con.commit()
        con.close()

    def tiles(self, user):
        from semantics.health import lab_health

        return {t["key"]: t for t in lab_health(user)["tiles"]}

    def test_apply_stores_timezone_classification_and_kpi_metadata(self):
        from semantics.models import CertifiedMetric, HealthTile, SemanticLayer

        self.assertEqual(SemanticLayer.objects.get(datasource=self.ds).timezone, "Asia/Riyadh")
        metric = CertifiedMetric.objects.get(datasource=self.ds, key="active")
        self.assertEqual((metric.kpi_id, metric.unit, metric.status, metric.observe, metric.owner,
                          metric.classes, metric.scoped),
                         ("PRJ-01", "count", "parity_checked", True, "PMO", [], True))
        bid = HealthTile.objects.get(datasource=self.ds, key="bid_value")
        self.assertEqual((bid.classes, bid.scoped), (["bid_confidential"], False))

    def test_the_classification_must_agree_with_the_sources_catalogue(self):
        self.assertEqual(verify_classification(self.ds, self.spec), [])
        drifted = dict(self.spec, tables={**self.spec["tables"],
                                          "rfq_value": {"scope": "all_scope_only"}})
        errs = verify_classification(self.ds, drifted)
        self.assertTrue(any("'rfq_value'" in e and "in the catalogue" in e for e in errs), errs)

    def test_an_uncleared_viewer_gets_restricted_and_the_sql_never_runs(self):
        tiles = self.tiles(self.viewer)
        self.assertEqual(tiles["active"]["value"], 2)       # p1, p2 (p4 is excluded)
        self.assertTrue(tiles["bid_value"]["restricted"])
        self.assertIsNone(tiles["bid_value"]["value"])
        self.assertEqual(tiles["bid_value"]["sql"], "")

    @override_settings(DIS_CLASS_CLEARANCE={"bid_confidential": "Bid confidential"})
    def test_the_clearance_group_sees_it_and_the_cache_keeps_them_apart(self):
        self.assertTrue(self.tiles(self.viewer)["bid_value"]["restricted"])
        cleared = get_user_model().objects.create_user(username="c", password="x", role="viewer")
        cleared.groups.add(Group.objects.create(name="Bid confidential"))
        # no cache.clear(): the clearance is part of the key, so this is a new entry
        self.assertEqual(self.tiles(cleared)["bid_value"]["value"], 100.0)
        self.assertTrue(self.tiles(self.viewer)["bid_value"]["restricted"])
        self.assertEqual(self.tiles(self.admin)["bid_value"]["value"], 100.0)

    def test_a_restored_copy_reports_snapshot_then_stale(self):
        from replication.freshness import describe

        self.ds.options = {**self.ds.options,
                           "freshness_sql": "SELECT data_as_of_riyadh FROM v_data_as_of"}
        self.ds.save()
        self.assertEqual(describe(self.ds).kind, "unknown")          # no row yet
        riyadh = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)
        for hours_ago, kind in ((2, "snapshot"), (30, "stale")):
            cache.clear()
            self.sql("DELETE FROM v_data_as_of")
            self.sql(f"INSERT INTO v_data_as_of VALUES "
                     f"('{(riyadh - timedelta(hours=hours_ago)):%Y-%m-%dT%H:%M:%S}')")
            fresh = describe(self.ds)
            self.assertEqual(fresh.kind, kind, fresh.detail)
            self.assertTrue(fresh.data_as_of.endswith("+03:00"), fresh.data_as_of)

    def test_observations_are_recorded_once_per_snapshot(self):
        from semantics.models import KpiObservation

        out = StringIO()
        call_command("record_kpi_observations", datasource=["Rpt"], stdout=out)
        self.assertIn("Recorded 3 observation(s)", out.getvalue())   # 2 tiles + 1 metric
        call_command("record_kpi_observations", datasource=["Rpt"], stdout=StringIO())
        self.assertEqual(KpiObservation.objects.count(), 3)
        self.ds.options = {**self.ds.options, "restore_id": "r-2"}
        self.ds.save()
        call_command("record_kpi_observations", datasource=["Rpt"], stdout=StringIO())
        self.assertEqual(KpiObservation.objects.filter(restore_id="r-2").count(), 3)
        bid = KpiObservation.objects.get(key="bid_value", restore_id="r-2")
        self.assertEqual(bid.value, 100.0)     # the recorder is the system: no clearance gap

    def test_parity_passes_and_then_catches_a_difference(self):
        out = StringIO()
        try:
            call_command("check_metric_parity", datasource="Rpt", file=str(self.spec_file),
                         stdout=out)
        except CommandError as exc:
            self.fail(f"{exc}\n{out.getvalue()}")
        self.assertIn("1 KPI(s) checked, 0 not matching.", out.getvalue())
        self.sql("UPDATE exec_summary SET value = 3 WHERE kpi_id = 'PRJ-01'")
        with self.assertRaisesMessage(CommandError, "1 certified KPI(s) differ"):
            call_command("check_metric_parity", datasource="Rpt", file=str(self.spec_file),
                         stdout=StringIO())
