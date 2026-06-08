"""Known-answer tests for the statistical engine (analytics.stats_tests) and the
catalog dispatcher. Pure (SimpleTestCase) — they build DataFrames directly, so no
DB is needed."""
import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from analytics import (
    anomaly,
    forecasting_ext,
    relationships,
    rootcause,
    semantic,
    stats_catalog,
)
from analytics import stats_tests as S
from analytics.engine import AnalyticsError


def _rng(seed=0):
    return np.random.default_rng(seed)


class DescriptiveTests(SimpleTestCase):
    def test_descriptive_known_values(self):
        df = pd.DataFrame({"x": list(range(1, 101))})  # mean 50.5
        out = S.descriptive_stats(df, "x")
        self.assertEqual(out["n"], 100)
        self.assertAlmostEqual(out["mean"], 50.5, places=3)
        self.assertAlmostEqual(out["median"], 50.5, places=3)

    def test_normality_separates_normal_from_skewed(self):
        normal = pd.DataFrame({"x": _rng(1).normal(0, 1, 400)})
        skewed = pd.DataFrame({"x": _rng(2).exponential(1.0, 400)})
        self.assertTrue(S.normality_test(normal, "x")["is_normal"])
        self.assertFalse(S.normality_test(skewed, "x")["is_normal"])


class ComparisonTests(SimpleTestCase):
    def test_one_sample_t(self):
        df = pd.DataFrame({"x": list(range(1, 101))})  # mean 50.5
        self.assertGreater(S.t_test_one_sample(df, "x", 50.5)["p_value"], 0.05)
        self.assertTrue(S.t_test_one_sample(df, "x", 40)["significant"])

    def test_two_sample_t_by_group_and_columns(self):
        df = pd.DataFrame({
            "v": list(_rng(3).normal(10, 1, 50)) + list(_rng(4).normal(20, 1, 50)),
            "g": ["A"] * 50 + ["B"] * 50,
        })
        self.assertTrue(S.t_test_two_sample(df, "v", group_column="g")["significant"])
        # Identical columns by construction → means equal → never significant.
        df2 = pd.DataFrame({"a": list(range(50)), "b": list(range(50))})
        self.assertFalse(S.t_test_two_sample(df2, "a", column2="b")["significant"])

    def test_anova_detects_group_differences(self):
        df = pd.DataFrame({
            "v": (list(_rng(7).normal(10, 1, 40)) + list(_rng(8).normal(15, 1, 40))
                  + list(_rng(9).normal(20, 1, 40))),
            "g": ["G1"] * 40 + ["G2"] * 40 + ["G3"] * 40,
        })
        out = S.anova_oneway(df, "v", "g")
        self.assertTrue(out["significant"])
        self.assertIn("tukey_hsd", out)  # post-hoc ran (3 groups, significant)
        self.assertEqual(out["groups"], 3)

    def test_anova_no_difference(self):
        # Three identical groups by construction → equal means → not significant.
        df = pd.DataFrame({
            "v": list(range(40)) * 3,
            "g": (["A"] * 40 + ["B"] * 40 + ["C"] * 40),
        })
        self.assertFalse(S.anova_oneway(df, "v", "g")["significant"])

    def test_f_test_variances(self):
        df = pd.DataFrame({"a": _rng(11).normal(0, 1, 100),
                          "b": _rng(12).normal(0, 5, 100)})
        out = S.f_test_variances(df, "a", column2="b")
        self.assertFalse(out["equal_variances"])  # variances differ 1 vs 25


class RelationshipTests(SimpleTestCase):
    def test_correlation_perfect_and_methods(self):
        df = pd.DataFrame({"x": range(50), "y": [2 * i + 1 for i in range(50)]})
        for method in ("pearson", "spearman", "kendall"):
            out = S.correlation_test(df, "x", "y", method)
            self.assertGreater(out["coefficient"], 0.99)
            self.assertTrue(out["significant"])

    def test_chi_square_dependent_vs_independent(self):
        dep = pd.DataFrame({"a": ["X"] * 50 + ["Y"] * 50,
                           "b": ["P"] * 45 + ["Q"] * 5 + ["P"] * 5 + ["Q"] * 45})
        self.assertTrue(S.chi_square_test(dep, "a", "b")["significant"])

    def test_linear_regression_recovers_slope(self):
        df = pd.DataFrame({"x": range(60), "y": [3 * i + 5 for i in range(60)]})
        out = S.regression(df, "y", x_column="x", kind="linear")
        self.assertGreater(out["r_squared"], 0.999)
        x_coef = next(c["coef"] for c in out["coefficients"] if c["term"] == "x")
        self.assertAlmostEqual(x_coef, 3.0, places=2)

    def test_polynomial_regression(self):
        df = pd.DataFrame({"x": range(-20, 20), "y": [i * i for i in range(-20, 20)]})
        out = S.regression(df, "y", x_column="x", kind="polynomial", degree=2)
        self.assertGreater(out["r_squared"], 0.999)

    def test_multiple_linear_regression(self):
        rng = _rng(13)
        x1, x2 = rng.normal(size=200), rng.normal(size=200)
        df = pd.DataFrame({"x1": x1, "x2": x2, "y": 2 * x1 - 3 * x2 + 1})
        out = S.regression(df, "y", x_columns=["x1", "x2"], kind="linear")
        self.assertGreater(out["r_squared"], 0.999)


class QualityTests(SimpleTestCase):
    def test_outliers_flags_extreme(self):
        df = pd.DataFrame({"x": [1, 2, 3, 4, 5, 4, 3, 2, 1, 100]})
        out = S.outlier_analysis(df, "x")
        self.assertGreaterEqual(out["outlier_count"], 1)
        self.assertEqual(out["outliers"][0]["value"], 100)

    def test_control_chart_detects_spike(self):
        data = [10 + 0.1 * (i % 3) for i in range(40)] + [999]
        out = S.control_chart(pd.DataFrame({"x": data}), "x")
        self.assertFalse(out["in_control"])
        self.assertGreaterEqual(out["violation_count"], 1)

    def test_process_capability_capable(self):
        df = pd.DataFrame({"x": _rng(14).normal(10, 0.5, 300)})
        out = S.process_capability(df, "x", lsl=7, usl=13)
        self.assertTrue(out["capable"])
        self.assertGreater(out["cpk"], 1.33)

    def test_trend_increasing(self):
        df = pd.DataFrame({"x": [i + _rng(15).normal(0, 0.1) for i in range(60)]})
        out = S.trend_analysis(df, "x")
        self.assertEqual(out["direction"], "increasing")


class CatalogTests(SimpleTestCase):
    def test_catalog_is_complete(self):
        cat = stats_catalog.catalog()
        names = {c["name"] for c in cat}
        self.assertEqual(names, set(stats_catalog.TESTS))
        for entry in cat:
            self.assertIn("params", entry)
            self.assertTrue(entry["label"])

    def test_dispatcher_runs_and_filters_params(self):
        df = pd.DataFrame({"x": range(50), "y": [2 * i for i in range(50)]})
        # extra junk param must be dropped, not crash
        out = stats_catalog.run_test(
            "correlation", df,
            {"column1": "x", "column2": "y", "method": "pearson", "junk": 1},
        )
        self.assertGreater(out["coefficient"], 0.99)

    def test_unknown_test_raises(self):
        with self.assertRaises(AnalyticsError):
            stats_catalog.run_test("nope", pd.DataFrame({"x": [1, 2, 3]}), {})


class AnomalyTests(SimpleTestCase):
    def test_isolation_forest_flags_injected_outlier(self):
        rng = _rng(21)
        normal = rng.normal(0, 1, (99, 2))
        data = np.vstack([normal, [[100.0, 100.0]]])  # last row is extreme
        df = pd.DataFrame(data, columns=["a", "b"])
        out = anomaly.isolation_forest(df, contamination=0.03)
        self.assertGreaterEqual(out["anomaly_count"], 1)
        self.assertIn(99, [r["row"] for r in out["anomalies"]])

    def test_lof_runs(self):
        rng = _rng(22)
        df = pd.DataFrame(rng.normal(0, 1, (120, 3)), columns=["a", "b", "c"])
        out = anomaly.local_outlier_factor(df, contamination=0.05)
        self.assertEqual(out["test"], "local_outlier_factor")
        self.assertGreaterEqual(out["anomaly_count"], 1)


class RootCauseTests(SimpleTestCase):
    def test_driver_analysis_finds_the_driver(self):
        rng = _rng(23)
        machine = rng.choice(["A", "B"], 400)
        p = np.where(machine == "B", 0.8, 0.1)
        status = np.where(rng.random(400) < p, "bad", "ok")
        noise = rng.choice(["x", "y", "z"], 400)
        df = pd.DataFrame({"machine": machine, "noise": noise, "status": status})
        out = rootcause.driver_analysis(
            df, "status", target_value="bad", feature_columns=["machine", "noise"])
        b_factor = next((f for f in out["top_factors"]
                         if f["factor"] == "machine" and f["value"] == "B"), None)
        self.assertIsNotNone(b_factor)
        self.assertGreater(b_factor["lift"], 1.5)
        self.assertEqual(out["feature_importance"][0]["factor"], "machine")

    def test_cluster_separates_two_blobs(self):
        rng = _rng(24)
        a = rng.normal(0, 0.5, (100, 2))
        b = rng.normal(10, 0.5, (100, 2))
        df = pd.DataFrame(np.vstack([a, b]), columns=["f1", "f2"])
        out = rootcause.cluster_analysis(df, method="kmeans", k=2)
        self.assertEqual(out["n_clusters"], 2)
        self.assertGreater(out["silhouette"], 0.7)

    def test_association_rules_finds_cooccurrence(self):
        rng = _rng(25)
        df = pd.DataFrame({
            "a": ["X"] * 100 + ["Y"] * 100,
            "b": ["P"] * 100 + ["Q"] * 100,  # a=X ⟺ b=P
            "c": list(rng.choice(["m", "n"], 200)),
        })
        out = rootcause.association_rules_mining(
            df, columns=["a", "b", "c"], min_support=0.1, min_confidence=0.8)
        self.assertGreater(out["rule_count"], 0)
        hit = any("a_X" in r["antecedents"] and "b_P" in r["consequents"]
                  for r in out["rules"])
        self.assertTrue(hit)

    def test_outcome_must_exist(self):
        df = pd.DataFrame({"t": ["ok"] * 10, "f": ["a"] * 5 + ["b"] * 5})
        with self.assertRaises(AnalyticsError):
            rootcause.driver_analysis(df, "t", target_value="missing")


class ForecastMetricTests(SimpleTestCase):
    def test_forecasts_increasing_trend(self):
        dates = pd.date_range("2023-01-01", periods=24, freq="MS")
        df = pd.DataFrame({"d": dates, "v": list(range(24))})
        out = forecasting_ext.forecast_metric(
            df, "d", value_column="v", agg="mean", freq="M", periods=3)
        self.assertEqual(out["trend"]["direction"], "increasing")
        self.assertEqual(len(out["forecast"]), 3)
        self.assertEqual(len(out["history"]), 24)

    def test_group_by_series(self):
        dates = pd.date_range("2023-01-01", periods=24, freq="MS")
        df = pd.DataFrame({
            "d": list(dates) * 2,
            "g": ["A"] * 24 + ["B"] * 24,
            "v": list(range(24)) + list(range(24)),
        })
        out = forecasting_ext.forecast_metric(
            df, "d", agg="count", freq="M", periods=2, group_by="g")
        self.assertEqual(out["series_count"], 2)
        self.assertTrue(all("forecast" in s for s in out["series"]))

    def test_bad_freq_raises(self):
        df = pd.DataFrame({"d": pd.date_range("2023-01-01", periods=5), "v": range(5)})
        with self.assertRaises(AnalyticsError):
            forecasting_ext.forecast_metric(df, "d", freq="X")


class ContextualTests(SimpleTestCase):
    def test_discovers_numeric_correlation(self):
        rng = _rng(30)
        x = rng.normal(size=200)
        df = pd.DataFrame({"x": x, "y": 2 * x + rng.normal(0, 0.1, 200),
                           "c": rng.choice(["a", "b"], 200)})
        out = relationships.discover_relationships(df)
        self.assertTrue(any(set(r["columns"]) == {"x", "y"} and
                            r["type"] == "numeric_correlation"
                            for r in out["relationships"]))

    def test_discovers_category_effect(self):
        rng = _rng(31)
        cat = rng.choice(["A", "B"], 200)
        val = np.where(cat == "A", 0.0, 10.0) + rng.normal(0, 0.5, 200)
        df = pd.DataFrame({"grp": cat, "measure": val})
        out = relationships.discover_relationships(df, min_eta=0.05)
        self.assertTrue(any(r["type"] == "category_drives_numeric"
                            for r in out["relationships"]))

    def test_suggest_links_normalises_names(self):
        meta = [{"id": 1, "name": "A", "columns": ["sample_id", "ph"]},
                {"id": 2, "name": "B", "columns": ["Sample_ID", "tat"]}]
        out = relationships.suggest_links(meta)
        self.assertGreaterEqual(out["link_count"], 1)
        self.assertEqual(out["links"][0]["dataset_count"], 2)


class NLToSQLGoldenTests(SimpleTestCase):
    """Golden set for the natural-language query engine (analytics.semantic).
    Each case pins the *interpretation* (aggregation / measure / group-by /
    filter) and the *computed answer* for a fixed dataframe, so a regression in
    intent-parsing or execution fails CI rather than silently degrading."""

    def _df(self):
        return pd.DataFrame({
            "product": ["PROPANE", "PROPANE", "STEAM", "STEAM", "DIESEL"],
            "result_value": [10.0, 20.0, 5.0, 7.0, 100.0],
            "status": ["pass", "fail", "pass", "pass", "fail"],
        })

    def test_count_total_is_scalar(self):
        out = semantic.answer_question(self._df(), "how many rows are there?")
        self.assertTrue(out["understood"])
        self.assertEqual(out["interpretation"]["aggregation"], "count")
        self.assertIsNone(out["interpretation"]["group_by"])
        self.assertEqual(out["scalar"], 5)

    def test_average_measure_by_group(self):
        out = semantic.answer_question(
            self._df(), "what is the average result value by product?")
        interp = out["interpretation"]
        self.assertEqual(interp["aggregation"], "mean")
        self.assertEqual(interp["measure"], "result_value")
        self.assertEqual(interp["group_by"], "product")
        means = {k: v for k, v in out["result"]["rows"]}
        self.assertEqual(means["PROPANE"], 15)   # (10+20)/2
        self.assertEqual(means["STEAM"], 6)      # (5+7)/2
        self.assertEqual(means["DIESEL"], 100)
        # default sort is descending → DIESEL is the highlighted top row.
        self.assertEqual(out["result"]["rows"][0][0], "DIESEL")

    def test_count_by_group(self):
        out = semantic.answer_question(self._df(), "count rows by product")
        self.assertEqual(out["interpretation"]["aggregation"], "count")
        self.assertEqual(out["interpretation"]["group_by"], "product")
        counts = {k: v for k, v in out["result"]["rows"]}
        self.assertEqual(counts["PROPANE"], 2)
        self.assertEqual(counts["STEAM"], 2)
        self.assertEqual(counts["DIESEL"], 1)

    def test_max_scalar(self):
        out = semantic.answer_question(
            self._df(), "what is the maximum result value?")
        self.assertEqual(out["interpretation"]["aggregation"], "max")
        self.assertEqual(out["scalar"], 100)

    def test_categorical_filter_narrows_rows(self):
        out = semantic.answer_question(
            self._df(), "average result value where status is fail")
        interp = out["interpretation"]
        self.assertEqual(interp["aggregation"], "mean")
        self.assertEqual(len(interp["filters"]), 1)
        self.assertEqual(out["rows_matched"], 2)   # PROPANE(fail)=20, DIESEL(fail)=100
        self.assertEqual(out["scalar"], 60)        # (20+100)/2

    def test_blank_question_rejected(self):
        with self.assertRaises(semantic.SemanticError):
            semantic.answer_question(self._df(), "   ")
