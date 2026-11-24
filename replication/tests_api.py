"""Lineage + semantics APIs: the pipeline and the certified layer, made visible."""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from connections.models import DataSource
from replication.models import ReplicationState
from semantics.models import CertifiedMetric, SemanticColumn, SemanticLayer, SemanticTable


class LineageApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="lab", password="x")
        self.client.force_authenticate(self.user)
        self.source = DataSource.objects.create(
            name="EGPC_DEV", source_type="mssql", database_name="EGPC_DEV",
            schema_name="dbo", owner=self.user)
        self.replica = DataSource.objects.create(
            name="EGPC_REPLICA", source_type="postgres", database_name="dis",
            schema_name="egpc_replica", owner=self.user)

    def _state(self, table, ago, error=""):
        at = timezone.now() - ago
        return ReplicationState.objects.create(
            source=self.source, target=self.replica, table_name=table, cadence="hourly",
            key_columns=["SAMPLE_NUMBER"], watermark_column="CHANGED_ON",
            high_watermark="2026-07-13 06:00:00", row_count=60000,
            last_full_at=at, last_incremental_at=at, last_error=error)

    def test_lineage_renders_the_pipeline(self):
        self._state("SAMPLE", timedelta(minutes=10))
        self._state("RESULT", timedelta(minutes=8))

        body = self.client.get("/api/lineage/").json()
        pipe = body["pipelines"][0]

        self.assertEqual(pipe["source"]["name"], "EGPC_DEV")
        self.assertEqual(pipe["target"]["name"], "EGPC_REPLICA")
        self.assertEqual([t["table"] for t in pipe["tables"]], ["RESULT", "SAMPLE"])
        self.assertEqual(pipe["freshness"]["freshness"], "replica")
        self.assertIsNotNone(pipe["freshness"]["data_as_of"])
        # Watermark + row count are what make the pipeline auditable rather than decorative.
        self.assertEqual(pipe["tables"][0]["high_watermark"], "2026-07-13 06:00:00")
        self.assertEqual(pipe["tables"][0]["row_count"], 60000)
        self.assertEqual([m["name"] for m in pipe["marts"]],
                         ["mart_results", "mart_samples", "mart_offspec"])

    def test_lineage_surfaces_a_stale_table(self):
        self._state("SAMPLE", timedelta(minutes=5))
        self._state("RESULT", timedelta(hours=9))

        pipe = self.client.get("/api/lineage/").json()["pipelines"][0]
        self.assertEqual(pipe["freshness"]["freshness"], "stale")
        self.assertEqual(pipe["stale_tables"], ["RESULT"])
        stale = next(t for t in pipe["tables"] if t["table"] == "RESULT")
        self.assertTrue(stale["stale"])

    def test_a_datasource_with_no_replica_renders_as_live_not_empty(self):
        body = self.client.get("/api/lineage/").json()
        self.assertEqual(body["pipelines"], [])
        kinds = {p["target"]["name"]: p["freshness"]["freshness"] for p in body["live_sources"]}
        self.assertEqual(kinds, {"EGPC_DEV": "live", "EGPC_REPLICA": "live"})

    def test_lineage_does_not_leak_another_users_datasources(self):
        other = get_user_model().objects.create_user(username="other", password="x")
        DataSource.objects.create(name="SECRET_LIMS", source_type="mssql",
                                  database_name="x", owner=other)
        body = self.client.get("/api/lineage/").json()
        names = {p["target"]["name"] for p in body["live_sources"]}
        self.assertNotIn("SECRET_LIMS", names)

    def test_freshness_endpoint_answers_for_one_datasource(self):
        self._state("SAMPLE", timedelta(minutes=10))
        body = self.client.get(f"/api/lineage/freshness/?datasource={self.replica.id}").json()
        self.assertEqual(body["freshness"], "replica")
        self.assertIsNotNone(body["data_as_of"])

    def test_lineage_requires_auth(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/lineage/").status_code, 401)


class SemanticsApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="qa", password="x")
        self.client.force_authenticate(self.user)
        self.ds = DataSource.objects.create(
            name="EGPC_DEV", source_type="mssql", database_name="EGPC_DEV",
            schema_name="dbo", owner=self.user)
        SemanticLayer.objects.create(datasource=self.ds, notes="Off-spec means IN_SPEC='F'.")
        t = SemanticTable.objects.create(datasource=self.ds, name="RESULT",
                                         description="Individual test results.")
        SemanticColumn.objects.create(table=t, name="NUMERIC_ENTRY", needs_cast=True,
                                      description="Numeric result stored as TEXT.")
        SemanticColumn.objects.create(table=t, name="IN_SPEC",
                                      encoding="T=in-spec, F=off-spec")
        CertifiedMetric.objects.create(
            datasource=self.ds, key="oos_total", name="Total out-of-spec results",
            sql="SELECT COUNT(*) AS n FROM {{table:RESULT}} WHERE IN_SPEC = 'F'",
            row_limit=None, synonyms=["how many off spec results"])
        from semantics import layer as layer_mod
        layer_mod.invalidate()

    def test_layers_index_reports_what_is_curated(self):
        body = self.client.get("/api/semantics/").json()
        entry = next(l for l in body["layers"] if l["datasource"]["name"] == "EGPC_DEV")
        self.assertTrue(entry["present"])
        self.assertEqual(entry["metrics"], 1)
        self.assertEqual(entry["columns"], 2)

    def test_detail_shows_both_authored_and_rendered_sql(self):
        """The portable form proves one definition serves every dialect; the rendered
        form is the query that actually runs. An auditor needs to see both."""
        body = self.client.get(f"/api/semantics/{self.ds.id}/").json()
        metric = body["metrics"][0]
        self.assertIn("{{table:RESULT}}", metric["sql_authored"])
        self.assertIn("dbo.RESULT", metric["sql_rendered"])       # MSSQL dialect
        self.assertNotIn("{{", metric["sql_rendered"])

    def test_detail_exposes_the_encodings_the_accuracy_rests_on(self):
        body = self.client.get(f"/api/semantics/{self.ds.id}/").json()
        cols = {c["name"]: c for c in body["tables"][0]["columns"]}
        self.assertEqual(cols["IN_SPEC"]["encoding"], "T=in-spec, F=off-spec")
        self.assertTrue(cols["NUMERIC_ENTRY"]["needs_cast"])
        self.assertIn("IN_SPEC='F'", body["notes"])

    def test_detail_404s_on_an_inaccessible_datasource(self):
        other = get_user_model().objects.create_user(username="nope", password="x")
        hidden = DataSource.objects.create(name="HIDDEN", source_type="mssql",
                                           database_name="x", owner=other)
        self.assertEqual(self.client.get(f"/api/semantics/{hidden.id}/").status_code, 404)
