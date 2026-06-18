"""Release-0 engine tests: unified forecasting, extended anomaly detection,
statistics enhancements, the AI-explain layer, and lazy-import safety. Pure
SimpleTestCase (build DataFrames directly), matching analytics.tests."""
import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from analytics import anomaly, explain
from analytics import forecasting_models as FM
from analytics import stats_tests as S
from analytics.engine import AnalyticsError


def _rng(seed=0):
    return np.random.default_rng(seed)


class ForecastEngineTests(SimpleTestCase):
    def _seasonal(self, n=72, seed=7):
        t = np.arange(n)
        return 100 + 0.8 * t + 12 * np.sin(2 * np.pi * t / 12) + _rng(seed).normal(0, 2, n)

    def test_detect_season_from_freq_and_acf(self):
        y = self._seasonal()
        self.assertEqual(FM.detect_season(y, "M"), 12)
        self.assertGreaterEqual(FM.detect_season(y) or 0, 6)  # ACF finds ~yearly

    def test_auto_selects_and_builds_leaderboard(self):
        out = FM.run_forecast(self._seasonal(), periods=6, methods="auto", freq="M")
        self.assertEqual(out["season"], 12)
        self.assertIn(out["selected"], out["methods_run"])
        # the chosen model on a seasonal series should be a seasonal one
        self.assertIn(out["selected"], {"holt_winters", "sarima", "arima"})
        # leaderboard carries finite accuracy for the runnable methods
        ok = [s for s in out["scores"].values() if s.get("ok")]
        self.assertTrue(ok and all(s["smape"] is not None for s in ok))
        chosen = out["forecast"]
        self.assertEqual(len(chosen["forecast"]), 6)
        self.assertIsNotNone(chosen["lower"])
        self.assertIn(chosen["trend"]["direction"], {"increasing", "decreasing", "flat"})

    def test_missing_optional_lib_is_skipped_not_failed(self):
        out = FM.run_forecast(self._seasonal(), periods=4, methods="auto", freq="M")
        # whichever of xgboost/prophet/lstm aren't importable here are *skipped*,
        # never crashing the run.
        for m in out["methods_skipped"]:
            self.assertIn(m, FM.REGISTRY)

    def test_explicit_method_and_short_series_guard(self):
        out = FM.run_forecast(self._seasonal(), periods=4, methods="holt", freq="M")
        self.assertEqual(out["selected"], "holt")
        with self.assertRaises(AnalyticsError):
            FM.run_forecast([1.0, 2.0, 3.0], periods=3)  # < 5 points


class AnomalyDetectTests(SimpleTestCase):
    def _mv(self):
        X = _rng(0).normal(0, 1, (300, 3))
        X[5] = [8, 8, 8]; X[100] = [-7, 6, -7]; X[200] = [9, -9, 0]
        return pd.DataFrame(X, columns=["pH", "turb", "cond"])

    def test_multivariate_contributions_and_projection(self):
        for method in ("isolation_forest", "elliptic_envelope", "auto"):
            r = anomaly.detect(self._mv(), scope="multivariate", method=method, contamination=0.03)
            self.assertGreater(r["anomaly_count"], 0)
            self.assertIn("pca_projection", r)
            self.assertTrue(r["anomalies"][0]["contributions"])  # per-feature drivers

    def test_robust_modified_zscore(self):
        df = pd.DataFrame({"v": np.r_[_rng(1).normal(10, 1, 200), [60, -40]]})
        r = anomaly.detect(df, scope="univariate", method="mad", value_column="v")
        self.assertEqual(r["test"], "modified_zscore")
        self.assertGreaterEqual(r["anomaly_count"], 2)

    def test_stl_seasonal_residual(self):
        t = np.arange(120)
        ts = 50 + 10 * np.sin(2 * np.pi * t / 12) + _rng(2).normal(0, 1, 120)
        ts[60] += 30
        df = pd.DataFrame({"val": ts})
        r = anomaly.detect(df, scope="series", method="stl_residual", value_column="val", period=12)
        self.assertEqual(r["period"], 12)
        self.assertEqual(len(r["resid"]), 120)
        self.assertGreaterEqual(r["anomaly_count"], 1)


class StatsEnhancementTests(SimpleTestCase):
    def test_batch_descriptive(self):
        df = pd.DataFrame({"a": range(100), "b": _rng(3).normal(0, 1, 100), "txt": ["x"] * 100})
        out = S.batch_descriptive(df)
        self.assertEqual(out["test"], "batch_descriptive")
        self.assertGreaterEqual(out["n_columns"], 2)  # a, b (txt excluded)

    def test_two_sample_effect_size_and_assumptions(self):
        df = pd.DataFrame({
            "v": list(_rng(3).normal(10, 1, 60)) + list(_rng(4).normal(13, 1, 60)),
            "g": ["A"] * 60 + ["B"] * 60,
        })
        out = S.t_test_two_sample(df, "v", group_column="g", check_assumptions=True)
        self.assertIn("cohens_d", out)
        self.assertIn("assumptions", out)
        self.assertEqual(len(out["group1"]["ci"]), 2)

    def test_check_assumptions_standalone(self):
        df = pd.DataFrame({"v": _rng(5).normal(0, 1, 80), "g": ["A"] * 40 + ["B"] * 40})
        out = S.check_assumptions(df, value_column="v", group_column="g")
        self.assertEqual(out["test"], "assumption_checks")
        self.assertIn(out["recommended"], {"parametric", "nonparametric"})


class ExplainTests(SimpleTestCase):
    def test_explain_never_raises_and_is_structured(self):
        result = {"test": "isolation_forest", "n": 100, "anomaly_count": 5,
                  "anomalies": [{"row": 3, "score": -0.4, "values": {"pH": 9.1}}]}
        out = explain.explain_result("anomaly", result)
        self.assertIn("answer", out)
        self.assertIn("configured", out)
        self.assertIsInstance(out["suggestions"], list)

    def test_build_context_is_compact(self):
        ctx = explain.build_context("forecast", {
            "selected": "arima", "season": 12, "periods": 6, "metric": "smape",
            "scores": {"arima": {"ok": True, "smape": 4.2}},
            "forecast": {"history": [1, 2, 3], "forecast": [4, 5], "trend": {"direction": "increasing"}},
        })
        self.assertIn("arima", ctx)
        self.assertLessEqual(len(ctx), 6000)


class LazyImportSafetyTests(SimpleTestCase):
    def test_numpy_pinned_below_2(self):
        self.assertLess(int(np.__version__.split(".")[0]), 2)

    def test_forecasting_module_top_level_imports_are_light(self):
        # The engine must import with only numpy/pandas/statsmodels/sklearn — the
        # heavy/optional libs (torch, xgboost, neuralprophet) are imported lazily.
        import importlib
        import sys
        for mod in ("torch", "xgboost", "neuralprophet"):
            self.assertNotIn(mod, getattr(FM, "__dict__", {}),
                             f"{mod} must not be a module-level import in forecasting_models")
        importlib.reload(FM)  # re-import must not require optional libs
        self.assertIn("naive", FM.REGISTRY)
