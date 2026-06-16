"""Replication tests.

The headline test runs the whole loop offline on SQLite (source → replica):
full load → modify source → incremental CHANGED_ON upsert → rebuild marts, and
asserts the marts decode the LabWare quirks. Dialect fragments and the stale
detector are unit-tested directly. (The MSSQL→Postgres prod path is validated on
the server via `manage.py replicate_lims`; it needs those engines.)
"""
import os
import tempfile

import pandas as pd
from sqlalchemy import create_engine, text
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from connections.engine import get_engine
from connections.models import DataSource
from replication.marts import build_marts, mart_definitions
from replication.models import ReplicationState
from replication.sql_dialect import is_off_spec, quote_ident, safe_float, status_case

TEST_TABLES = [
    {"table": "SAMPLE", "keys": ["SAMPLE_NUMBER"], "watermark": "CHANGED_ON", "cadence": "hourly"},
    {"table": "RESULT", "keys": ["SAMPLE_NUMBER", "RESULT_NUMBER"], "watermark": "CHANGED_ON", "cadence": "hourly"},
]


def _write_sqlite(path, tables):
    eng = create_engine(f"sqlite:///{path}")
    for name, df in tables.items():
        df.to_sql(name, eng, index=False, if_exists="replace")
    eng.dispose()


def _touch_sqlite(path):
    eng = create_engine(f"sqlite:///{path}")
    with eng.connect() as conn:
        conn.execute(text("SELECT 1"))
    eng.dispose()


class DialectFragmentTests(SimpleTestCase):
    def test_safe_float_postgres_has_numeric_guard(self):
        sql = safe_float('r."NUMERIC_ENTRY"', "postgres")
        self.assertIn("double precision", sql)
        self.assertIn("~", sql)  # regex guard so non-numeric text -> NULL

    def test_safe_float_mssql_try_convert(self):
        self.assertEqual(safe_float('r."NUMERIC_ENTRY"', "mssql"),
                         'TRY_CONVERT(float, r."NUMERIC_ENTRY")')

    def test_status_case_decodes_codes(self):
        sql = status_case('s."STATUS"', "postgres")
        self.assertIn("WHEN 'R' THEN 'Rejected'", sql)
        self.assertIn("WHEN 'A' THEN 'Authorized'", sql)

    def test_is_off_spec(self):
        self.assertEqual(is_off_spec('r."IN_SPEC"'), "CASE WHEN r.\"IN_SPEC\" = 'F' THEN 1 ELSE 0 END")

    def test_quote_ident(self):
        self.assertEqual(quote_ident("X", "mssql"), "[X]")
        self.assertEqual(quote_ident("X", "postgres"), '"X"')
        self.assertEqual(quote_ident("X", "mysql"), "`X`")


class MartSqlTests(SimpleTestCase):
    def test_postgres_marts_are_well_formed(self):
        defs = dict(mart_definitions("postgres", "egpc_replica"))
        self.assertEqual(set(defs), {"mart_results", "mart_samples", "mart_offspec"})
        # schema-qualified + quoted raw refs, the join, and the decodes are present
        self.assertIn('"egpc_replica"."RESULT"', defs["mart_results"])
        self.assertIn('"SAMPLE_NUMBER"', defs["mart_results"])
        self.assertIn("double precision", defs["mart_results"])  # safe cast
        self.assertIn("is_off_spec", defs["mart_results"])
        self.assertIn("WHEN 'R' THEN 'Rejected'", defs["mart_samples"])
        self.assertIn("= 'F'", defs["mart_offspec"])  # off-spec filter


class StaleStateTests(SimpleTestCase):
    def _state(self, **kw):
        defaults = dict(cadence="hourly", last_full_at=timezone.now(),
                        last_incremental_at=timezone.now(), last_error="")
        defaults.update(kw)
        return ReplicationState(table_name="T", **defaults)

    def test_fresh_not_stale(self):
        self.assertFalse(self._state().is_stale())

    def test_overdue_is_stale(self):
        old = timezone.now() - timedelta(hours=3)
        self.assertTrue(self._state(last_full_at=old, last_incremental_at=old).is_stale(factor=2))

    def test_error_is_stale(self):
        self.assertTrue(self._state(last_error="boom").is_stale())

    def test_never_synced_is_stale(self):
        self.assertTrue(self._state(last_full_at=None, last_incremental_at=None).is_stale())


@override_settings(DSE_REPLICATION_TABLES=TEST_TABLES)
class ReplicationE2ETests(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src_path = os.path.join(self.dir, "src.sqlite")
        self.tgt_path = os.path.join(self.dir, "tgt.sqlite")
        self.user = get_user_model().objects.create_user(username="repl_t", password="x")

        sample = pd.DataFrame([
            {"SAMPLE_NUMBER": 1, "TEXT_ID": "S1", "STATUS": "A", "IN_SPEC": "T",
             "PRODUCT": "DIESEL", "SAMPLE_TYPE": "SCHEDULED",
             "LOGIN_DATE": "2026-01-01 08:00:00", "DATE_COMPLETED": "2026-01-01 12:00:00",
             "CHANGED_ON": "2026-01-01 12:00:00"},
            {"SAMPLE_NUMBER": 2, "TEXT_ID": "S2", "STATUS": "R", "IN_SPEC": "F",
             "PRODUCT": "PROPANE", "SAMPLE_TYPE": "CUSTOMER",
             "LOGIN_DATE": "2026-01-02 08:00:00", "DATE_COMPLETED": "2026-01-02 20:00:00",
             "CHANGED_ON": "2026-01-02 20:00:00"},
        ])
        result = pd.DataFrame([
            {"SAMPLE_NUMBER": 1, "RESULT_NUMBER": 1, "ANALYSIS": "SULFUR", "NAME": "Sulfur",
             "NUMERIC_ENTRY": "10.5", "UNITS": "ppm", "IN_SPEC": "T", "ENTERED_BY": "5227",
             "ENTERED_ON": "2026-01-01 11:00:00", "CHANGED_ON": "2026-01-01 11:00:00"},
            {"SAMPLE_NUMBER": 2, "RESULT_NUMBER": 1, "ANALYSIS": "FLASH", "NAME": "Flash",
             "NUMERIC_ENTRY": "55.0", "UNITS": "C", "IN_SPEC": "F", "ENTERED_BY": "5300",
             "ENTERED_ON": "2026-01-02 19:00:00", "CHANGED_ON": "2026-01-02 19:00:00"},
        ])
        _write_sqlite(self.src_path, {"SAMPLE": sample, "RESULT": result})
        _touch_sqlite(self.tgt_path)  # build_url(sqlite) requires the file to exist

        self.source = DataSource.objects.create(
            name="src-lims", source_type="sqlite", database_name=self.src_path,
            options={"path": self.src_path}, owner=self.user)
        self.target = DataSource.objects.create(
            name="replica", source_type="sqlite", database_name=self.tgt_path,
            options={"path": self.tgt_path}, owner=self.user)

    def tearDown(self):
        try:
            get_engine(self.source).dispose()
            get_engine(self.target).dispose()
        except Exception:
            pass

    def _scalar(self, sql):
        with get_engine(self.target).connect() as conn:
            return conn.execute(text(sql)).scalar()

    def test_full_then_incremental_then_marts(self):
        from replication.loader import replicate_all, replicate_table
        from replication.models import ReplicationState

        # 1) FULL load
        res = replicate_all(self.source, self.target, mode="full")
        self.assertTrue(all(not r.get("error") for r in res), res)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM "SAMPLE"'), 2)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM "RESULT"'), 2)
        rstate = ReplicationState.objects.get(table_name="RESULT")
        self.assertEqual(rstate.high_watermark, "2026-01-02 19:00:00")

        # 2) Modify source: update result (1,1) value + bump CHANGED_ON; add (1,2)
        new_result = pd.DataFrame([
            {"SAMPLE_NUMBER": 1, "RESULT_NUMBER": 1, "ANALYSIS": "SULFUR", "NAME": "Sulfur",
             "NUMERIC_ENTRY": "12.0", "UNITS": "ppm", "IN_SPEC": "T", "ENTERED_BY": "5227",
             "ENTERED_ON": "2026-02-01 09:00:00", "CHANGED_ON": "2026-02-01 09:00:00"},
            {"SAMPLE_NUMBER": 2, "RESULT_NUMBER": 1, "ANALYSIS": "FLASH", "NAME": "Flash",
             "NUMERIC_ENTRY": "55.0", "UNITS": "C", "IN_SPEC": "F", "ENTERED_BY": "5300",
             "ENTERED_ON": "2026-01-02 19:00:00", "CHANGED_ON": "2026-01-02 19:00:00"},
            {"SAMPLE_NUMBER": 1, "RESULT_NUMBER": 2, "ANALYSIS": "DENSITY", "NAME": "Density",
             "NUMERIC_ENTRY": "0.84", "UNITS": "g/ml", "IN_SPEC": "T", "ENTERED_BY": "5227",
             "ENTERED_ON": "2026-02-01 09:05:00", "CHANGED_ON": "2026-02-01 09:05:00"},
        ])
        _write_sqlite(self.src_path, {"SAMPLE": pd.read_sql('SELECT * FROM "SAMPLE"', get_engine(self.source)),
                                      "RESULT": new_result})

        # 3) INCREMENTAL upsert (only CHANGED_ON > watermark rows move)
        rstate.refresh_from_db()
        out = replicate_table(rstate, mode="incremental")
        self.assertIsNone(out.get("error"), out)
        self.assertEqual(out["rows_pulled"], 2)              # (1,1) updated + (1,2) new
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM "RESULT"'), 3)
        self.assertEqual(self._scalar(
            'SELECT NUMERIC_ENTRY FROM "RESULT" WHERE SAMPLE_NUMBER=1 AND RESULT_NUMBER=1'),
            "12.0")
        rstate.refresh_from_db()
        self.assertEqual(rstate.high_watermark, "2026-02-01 09:05:00")

        # 4) MARTS — quirks decoded
        built = {b["mart"]: b["rows"] for b in build_marts(get_engine(self.target), "sqlite", None)}
        self.assertEqual(built["mart_results"], 3)
        self.assertEqual(built["mart_offspec"], 1)           # only the IN_SPEC='F' result
        # decoded numeric value (text -> float) reflects the incremental update
        self.assertAlmostEqual(self._scalar(
            "SELECT value FROM mart_results WHERE sample_number=1 AND result_number=1"), 12.0)
        # decoded status label + computed turnaround on the sample mart
        self.assertEqual(self._scalar(
            "SELECT status_label FROM mart_samples WHERE sample_number=2"), "Rejected")
        self.assertAlmostEqual(self._scalar(
            "SELECT tat_hours FROM mart_samples WHERE sample_number=1"), 4.0)
        self.assertEqual(self._scalar(
            "SELECT is_off_spec FROM mart_results WHERE sample_number=2 AND result_number=1"), 1)
