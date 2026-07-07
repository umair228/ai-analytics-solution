"""Dashboard-filter resilience: stale cached columns must self-heal, never
crash a widget, and inapplicable filters must be reported — not silently lied
about ("filtered" data that isn't)."""
import json
import os
import sqlite3
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from connections.models import DataSource
from datasets.models import Dataset
from datasets.views import DatasetViewSet
from querybuilder.models import QueryDefinition

User = get_user_model()

# An AGGREGATED query: lab_id / received_datetime exist in the SOURCE but are
# aggregated away — the output has only month + counters (like the user's
# "Daily Laboratory Performance" dataset after its query was edited).
AGG_SQL = """WITH base AS (
  SELECT *, DATE(received_datetime, 'start of month') AS received_month
  FROM data
)
SELECT received_month, COUNT(DISTINCT sample_id) AS total_samples
FROM base GROUP BY received_month"""


class FilterSelfHealTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = sqlite3.connect(cls.db_path)
        conn.execute("CREATE TABLE data (sample_id TEXT, lab_id TEXT, received_datetime TEXT)")
        conn.executemany("INSERT INTO data VALUES (?, ?, ?)", [
            ("S1", "LAB-GAS", "2025-01-05 08:00:00"),
            ("S2", "LAB-GAS", "2025-01-15 09:00:00"),
            ("S3", "LAB-OIL", "2025-02-02 10:00:00"),
        ])
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.unlink(cls.db_path)

    def setUp(self):
        self.user = User.objects.create_superuser("heal-tester", "h@x.co", "x")
        self.ds = DataSource.objects.create(
            name="heal-sqlite", source_type="sqlite", owner=self.user,
            options={"path": self.db_path})
        self.query = QueryDefinition.objects.create(
            name="agg", datasource=self.ds, mode=QueryDefinition.Mode.RAW,
            raw_sql=AGG_SQL, owner=self.user)
        # STALE cache: pretends the output still has the pre-edit columns.
        self.dataset = Dataset.objects.create(
            name="perf", query=self.query, owner=self.user,
            cached_columns=["received_date", "lab_id", "total_samples"],
            cached_rows=[["2025-01-05", "LAB-GAS", 1]], row_count=1)
        self.factory = APIRequestFactory()

    def _data(self, filters):
        view = DatasetViewSet.as_view({"get": "data"})
        req = self.factory.get(
            f"/x/?filters={json.dumps(filters)}")
        force_authenticate(req, user=self.user)
        resp = view(req, pk=self.dataset.pk)
        resp.render()
        return resp, json.loads(resp.content)

    def _explore(self, body):
        view = DatasetViewSet.as_view({"post": "explore"})
        req = self.factory.post("/x/", body, format="json")
        force_authenticate(req, user=self.user)
        resp = view(req, pk=self.dataset.pk)
        resp.render()
        return resp, json.loads(resp.content)

    def test_data_heals_stale_columns_and_reports_skipped(self):
        # The dashboard's exact failure: a dropdown filter on lab_id, which the
        # aggregated output no longer has → used to be a hard SQL error.
        resp, out = self._data([{"column": "lab_id", "type": "dropdown",
                                 "value": "LAB-GAS"}])
        self.assertEqual(resp.status_code, 200)
        self.assertIn("lab_id", out.get("skipped_filters", []))
        self.assertTrue(out.get("skipped_filters_hint"))
        self.assertEqual(out["row_count"], 2)  # Jan + Feb months, unfiltered
        # Cache healed to the true output columns.
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.cached_columns,
                         ["received_month", "total_samples"])

    def test_data_applies_filters_on_real_output_columns(self):
        # A filter on a REAL output column still applies after healing.
        resp, out = self._data([
            {"column": "received_date", "type": "date-range",
             "from": "2025-01-01", "to": "2025-12-31"},   # stale → skipped
            {"column": "received_month", "type": "dropdown",
             "value": "2025-01-01"},                       # real → applied
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(out.get("skipped_filters"), ["received_date"])
        self.assertEqual(out["row_count"], 1)
        self.assertEqual(out["rows"][0][1], 2)  # 2 samples in January

    def test_explore_heals_stale_columns(self):
        resp, out = self._explore({
            "mode": "count",
            "filters": [{"column": "lab_id", "type": "dropdown",
                         "value": "LAB-GAS"}],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(out["total_row_count"], 2)
        self.assertEqual(out.get("skipped_filters"), ["lab_id"])

    def test_spec_mode_reports_all_filters_skipped(self):
        # Builder-spec datasets can't take output filters — they must be
        # reported as skipped, not silently ignored while looking applied.
        spec_query = QueryDefinition.objects.create(
            name="spec", datasource=self.ds, mode=QueryDefinition.Mode.BUILDER,
            owner=self.user,
            spec={"base_table": {"schema": None, "name": "data"},
                  "columns": [{"table": "data", "column": "lab_id"}]})
        spec_ds = Dataset.objects.create(
            name="spec-ds", query=spec_query, owner=self.user,
            cached_columns=["lab_id"], cached_rows=[["LAB-GAS"]], row_count=1)
        view = DatasetViewSet.as_view({"get": "data"})
        req = self.factory.get(
            '/x/?filters=[{"column":"lab_id","type":"dropdown","value":"LAB-GAS"}]')
        force_authenticate(req, user=self.user)
        resp = view(req, pk=spec_ds.pk)
        resp.render()
        out = json.loads(resp.content)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(out.get("skipped_filters"), ["lab_id"])
        self.assertIn("visual-builder", out.get("skipped_filters_hint", ""))

    def test_malformed_filter_entries_degrade_cleanly(self):
        # Non-dict / column-less filter entries must not 500.
        resp, out = self._data([None, {"type": "dropdown", "value": "x"},
                                "garbage"])
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("skipped_filters", out)

    def test_declared_param_defaults_merge_under_live_params(self):
        # A parameterized query must run even when no live value is supplied.
        self.query.raw_sql = (
            "SELECT lab_id, COUNT(*) AS n FROM data "
            "WHERE (:lab_id = '' OR lab_id = :lab_id) GROUP BY lab_id")
        self.query.parameters = [{"name": "lab_id", "type": "text", "default": ""}]
        self.query.save()
        self.dataset.cached_columns = ["lab_id", "n"]
        self.dataset.save()

        view = DatasetViewSet.as_view({"get": "data"})
        # Live branch with NO value for :lab_id → declared default '' → both labs.
        # (the "_probe" key only forces the live-params code path; text() binds
        # ignore params that have no matching placeholder)
        req = self.factory.get('/x/?params={"_probe":"1"}')
        force_authenticate(req, user=self.user)
        resp = view(req, pk=self.dataset.pk); resp.render()
        self.assertEqual(json.loads(resp.content)["row_count"], 2)
        # Live param overrides the default → one lab.
        req = self.factory.get('/x/?params={"lab_id":"LAB-OIL"}')
        force_authenticate(req, user=self.user)
        resp = view(req, pk=self.dataset.pk); resp.render()
        out = json.loads(resp.content)
        self.assertEqual(out["row_count"], 1)
        self.assertEqual(out["rows"][0][0], "LAB-OIL")
