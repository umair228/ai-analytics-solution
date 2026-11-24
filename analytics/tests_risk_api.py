"""The /api/analytics/risk/ endpoint the Risk page calls, and the run it persists.

A risk model belongs in the saved-run history for the same reason a QC chart does:
"on this date we scored these samples, with this model, at this accuracy" is the
record an auditor asks for when a flagged sample was released anyway.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from analytics.models import AnalysisRun
from connections.models import DataSource
from datasets.models import Dataset
from querybuilder.models import QueryDefinition


def _rows(n=500, seed=5):
    rng = np.random.default_rng(seed)
    product = rng.choice(["Diesel", "Gasoline", "Kerosene"], n, p=[0.5, 0.3, 0.2])
    analyst = rng.choice(["a.hassan", "m.said"], n)
    p_fail = 0.03 + 0.25 * (product == "Kerosene") + 0.12 * (analyst == "m.said")
    failed = rng.random(n) < p_fail
    df = pd.DataFrame({
        "login_date": pd.date_range("2026-01-01", periods=n, freq="6h").astype(str),
        "product": product,
        "analyst": analyst,
        "in_spec": np.where(failed, "F", "T"),
    })
    df.loc[df.index[-30:], "in_spec"] = None      # 30 samples still in the lab
    return df.to_dict("records")


class RiskApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="qa", password="x")
        self.client.force_authenticate(self.user)
        src = DataSource.objects.create(name="lims", source_type="sqlite",
                                        owner=self.user, options={"path": "/tmp/x.sqlite"})
        q = QueryDefinition.objects.create(name="samples", datasource=src, owner=self.user,
                                           mode=QueryDefinition.Mode.RAW,
                                           raw_sql="SELECT * FROM sample")
        rows = _rows()
        self.ds = Dataset.objects.create(
            name="Samples", owner=self.user, query=q,
            cached_columns=list(rows[0].keys()), cached_rows=rows,
            row_count=len(rows), total_row_count=len(rows),
            # Without this, build_dataframe re-runs the query against the (absent)
            # source instead of using the cache.
            last_refreshed_at=timezone.now())

    def _post(self, **body):
        return self.client.post("/api/analytics/risk/",
                                {"dataset": self.ds.id, **body}, format="json")

    def test_it_returns_a_model_card_and_ranked_samples(self):
        r = self._post(target_column="in_spec", positive_value="F",
                       time_column="login_date")
        self.assertEqual(r.status_code, 200)
        out = r.json()["result"]

        self.assertIn("usable", out)
        self.assertIn("metrics", out)
        self.assertIn("leakage", out)
        self.assertIn("lift", out)
        self.assertIn("calibration", out)
        self.assertIn("permutation", out["importance_kind"])
        self.assertEqual(out["n_in_flight"], 30)
        self.assertEqual(len(out["at_risk"]), 30)
        self.assertEqual(out["split"]["kind"], "time-ordered")

    def test_the_run_is_saved_so_an_auditor_can_find_it(self):
        r = self._post(target_column="in_spec", positive_value="F",
                       time_column="login_date", name="Weekly OOS risk")
        run_id = r.json().get("run_id")
        self.assertIsNotNone(run_id)

        run = AnalysisRun.objects.get(pk=run_id)
        self.assertEqual(run.workflow, AnalysisRun.Workflow.RISK)
        self.assertEqual(run.name, "Weekly OOS risk")
        self.assertEqual(run.config["target_column"], "in_spec")
        self.assertEqual(run.status, AnalysisRun.Status.SUCCESS)

        # `usable` must be visible in the run LIST without opening the run: a model
        # that does not work has to be distinguishable from one that does.
        self.assertIn("usable", run.metrics)
        self.assertIn("roc_auc", run.metrics)
        self.assertIn("leakage_suspected", run.metrics)

    def test_a_missing_target_is_a_clean_400_not_a_500(self):
        r = self._post(target_column="")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["error"])

    def test_an_unpredictable_outcome_is_reported_as_unusable_over_the_wire(self):
        rng = np.random.default_rng(2)
        n = 300
        rows = pd.DataFrame({
            "login_date": pd.date_range("2026-01-01", periods=n, freq="6h").astype(str),
            "product": rng.choice(["A", "B"], n),
            "in_spec": rng.choice(["T", "F"], n, p=[0.85, 0.15]),
        })
        rows.loc[rows.index[-20:], "in_spec"] = None
        self.ds.cached_rows = rows.to_dict("records")
        self.ds.cached_columns = list(rows.columns)
        self.ds.save()

        out = self._post(target_column="in_spec", positive_value="F",
                         time_column="login_date").json()["result"]
        self.assertFalse(out["usable"])
        self.assertIn("does NOT work", out["interpretation"])
