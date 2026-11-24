"""Risk prediction — which sample is about to fail.

DIS could analyse off-spec results in every direction and could not answer the one
question a lab wants answered *before* the fact: is THIS sample likely to fail?

The most important tests here are the ones about not lying. A risk model that leaks
the future scores 0.99 and cannot run on a live sample; a model that rebalances the
classes returns a "70%" that does not mean 70%. Both fail safely and loudly, or the
model is worse than nothing — because a confidently wrong ranked list looks exactly
like a usable one.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from analytics.engine import AnalyticsError
from analytics.risk import predict_risk


def _lims(n=400, seed=7, in_flight=40, leak=False):
    """A LIMS-shaped frame: completed samples carry IN_SPEC, in-flight ones do not.

    Off-spec risk genuinely depends on product and analyst, so a working model must
    find that — and the noise columns must not drown it.
    """
    rng = np.random.default_rng(seed)
    products = rng.choice(["Diesel", "Gasoline", "Kerosene"], n, p=[0.5, 0.3, 0.2])
    analysts = rng.choice(["a.hassan", "m.said", "n.farouk"], n, p=[0.4, 0.4, 0.2])
    rush = rng.choice([0, 1], n, p=[0.8, 0.2])

    # The real signal: Kerosene and one analyst fail more; rush jobs fail more.
    p_fail = (0.03
              + 0.22 * (products == "Kerosene")
              + 0.15 * (analysts == "n.farouk")
              + 0.10 * rush)
    failed = rng.random(n) < p_fail

    df = pd.DataFrame({
        "sample_number": np.arange(1, n + 1),
        "login_date": pd.date_range("2026-01-01", periods=n, freq="6h"),
        "product": products,
        "analyst": analysts,
        "rush": rush,
        "prep_temp": rng.normal(20, 2, n).round(2),          # pure noise
        "in_spec": np.where(failed, "F", "T"),
    })
    # NB `df.index[-0:]` is `df.index[0:]` — i.e. EVERY row. Guard the zero case.
    open_rows = df.index[-in_flight:] if in_flight else df.index[:0]

    if leak:
        # The result value — known only AFTER the analysis. This is the column that
        # gives a leaky model its beautiful, useless AUC.
        df["result_value"] = np.where(failed, rng.normal(90, 1, n), rng.normal(10, 1, n))
        df.loc[open_rows, "result_value"] = np.nan

    # The in-flight samples are still in the lab: no verdict yet.
    df.loc[open_rows, "in_spec"] = None
    return df


class TrainsOnClosedScoresOpenTests(SimpleTestCase):
    """The design: one dataset, train on the completed rows, score the in-flight ones."""

    def test_it_learns_the_real_drivers_and_ranks_the_in_flight_samples(self):
        out = predict_risk(_lims(n=600), "in_spec", positive_value="F",
                           time_column="login_date")

        self.assertTrue(out["usable"])
        self.assertGreater(out["metrics"]["roc_auc"], 0.65)
        self.assertEqual(out["n_in_flight"], 40)
        self.assertEqual(len(out["at_risk"]), 40)

        # It must find the PLANTED signal: Kerosene and one analyst fail more.
        top = [f["feature"] for f in out["feature_importance"][:3]]
        self.assertTrue(any("product" in f or "analyst" in f for f in top), top)

        # Scores are ranked, and each carries the context needed to act on it.
        risks = [r["risk"] for r in out["at_risk"]]
        self.assertEqual(risks, sorted(risks, reverse=True))
        self.assertIn("product", out["at_risk"][0])

    def test_a_pure_noise_column_is_not_reported_as_a_driver(self):
        """`prep_temp` is random numbers. The model's built-in impurity importance
        ranked it 0.68 — the TOP driver of off-spec — because impurity importance is
        biased toward continuous, high-cardinality features. Permutation importance,
        measured on held-out data, correctly puts it at nothing.

        Telling a laboratory that a noise column drives its failures is not a small
        error. It is the error that ends the model's career.
        """
        out = predict_risk(_lims(n=600), "in_spec", positive_value="F",
                           time_column="login_date")

        self.assertIn("permutation", out["importance_kind"])
        ranked = {f["feature"]: f["importance"] for f in out["feature_importance"]}
        noise = ranked.get("prep_temp", 0.0)
        real = max(v for k, v in ranked.items()
                   if k.startswith("product_") or k.startswith("analyst_"))
        self.assertLess(noise, real / 3,
                        f"noise column scored {noise} against a real driver at {real}")

    def test_the_positive_class_defaults_to_the_RARE_outcome(self):
        """Off-spec is the rare one. Defaulting to the majority would silently model
        'did this sample behave normally', which nobody asked."""
        out = predict_risk(_lims(), "in_spec", time_column="login_date")
        self.assertEqual(out["positive_value"], "F")
        self.assertLess(out["base_rate"], 0.5)

    def test_a_time_ordered_split_is_used_when_a_date_is_given(self):
        """You are predicting the future, so the test set must BE the future."""
        out = predict_risk(_lims(), "in_spec", positive_value="F",
                           time_column="login_date")
        self.assertEqual(out["split"]["kind"], "time-ordered")

    def test_a_random_split_says_so_loudly(self):
        out = predict_risk(_lims(), "in_spec", positive_value="F")
        self.assertEqual(out["split"]["kind"], "random")
        self.assertIn("time-ordered split is the honest test", out["interpretation"])


class LeakageTests(SimpleTestCase):
    """The failure that produces a beautiful, useless model."""

    def test_a_column_absent_from_in_flight_samples_is_DROPPED(self):
        """result_value is empty until the analysis is done. A model leaning on it
        could never score a live sample — so it is leakage by construction, and no
        statistics are needed to know it."""
        out = predict_risk(_lims(leak=True), "in_spec", positive_value="F",
                           time_column="login_date")

        dropped = {d["column"] for d in out["leakage"]["dropped"]}
        self.assertIn("result_value", dropped)
        self.assertNotIn("result_value", out["features_used"])

        reason = next(d["reason"] for d in out["leakage"]["dropped"]
                      if d["column"] == "result_value")
        self.assertIn("in-flight", reason)
        self.assertIn("excluded as unusable at prediction time", out["interpretation"])

    def test_a_leaky_column_that_IS_present_is_flagged_as_suspicious(self):
        """If the leak is available at scoring time we cannot drop it on availability
        — so say plainly that the accuracy is not to be believed."""
        df = _lims()
        # A "review flag" that is really the answer wearing a hat.
        df["qa_flag"] = np.where(df["in_spec"] == "F", "REVIEW", "OK")
        df.loc[df["in_spec"].isna(), "qa_flag"] = "OK"

        out = predict_risk(df, "in_spec", positive_value="F", time_column="login_date")

        flagged = {s["column"] for s in out["leakage"]["suspicious"]}
        self.assertIn("qa_flag", flagged)
        self.assertIn("leakage", out["interpretation"].lower())
        self.assertIn("fiction", out["interpretation"])

    def test_an_identifier_column_is_dropped_not_memorised(self):
        out = predict_risk(_lims(), "in_spec", positive_value="F",
                           time_column="login_date")
        dropped = {d["column"] for d in out["leakage"]["dropped"]}
        self.assertIn("sample_number", dropped)


class HonestyTests(SimpleTestCase):
    def test_an_unpredictable_outcome_is_reported_as_unusable(self):
        """A ranked list of at-risk samples from a model that cannot predict looks
        exactly like a ranked list from one that can. Say it does not work."""
        rng = np.random.default_rng(3)
        n = 300
        df = pd.DataFrame({
            "login_date": pd.date_range("2026-01-01", periods=n, freq="6h"),
            "product": rng.choice(["A", "B", "C"], n),
            "noise": rng.normal(0, 1, n),
            "in_spec": rng.choice(["T", "F"], n, p=[0.85, 0.15]),   # pure coin flip
        })
        out = predict_risk(df, "in_spec", positive_value="F", time_column="login_date")

        self.assertFalse(out["usable"])
        self.assertIn("does NOT work", out["interpretation"])
        self.assertIn("Do not act on the risk scores", out["interpretation"])

    def test_probabilities_are_not_rebalanced_so_they_stay_calibrated(self):
        """Reweighting the classes would inflate every score away from the true base
        rate, and a risk score whose 70% does not mean 70% is not a risk score."""
        out = predict_risk(_lims(n=600), "in_spec", positive_value="F",
                           time_column="login_date")
        mean_risk = float(np.mean([r["risk"] for r in out["at_risk"]]))
        # The average predicted risk should sit in the neighbourhood of reality, not
        # up at ~50% the way a class-weighted model would push it.
        self.assertLess(mean_risk, 0.5)
        self.assertIsNotNone(out["metrics"]["brier"])
        self.assertTrue(out["calibration"])

    def test_lift_is_reported_in_the_shape_a_lab_can_spend(self):
        """A lab has capacity to re-check N samples, not a probability threshold."""
        out = predict_risk(_lims(n=600), "in_spec", positive_value="F",
                           time_column="login_date")
        self.assertEqual(out["lift"][0]["top_k"], 10)
        # Re-checking the riskiest samples must beat picking at random — that is the
        # entire proposition, and at some usable capacity it has to hold.
        best = max(row["lift"] for row in out["lift"])
        self.assertGreater(best, 1.5)
        self.assertIn("better than picking at random", out["interpretation"])


class GuardrailTests(SimpleTestCase):
    def test_too_few_labelled_samples_is_refused(self):
        df = _lims(n=30, in_flight=5)
        with self.assertRaises(AnalyticsError) as ctx:
            predict_risk(df, "in_spec", positive_value="F")
        self.assertIn("at least", str(ctx.exception))

    def test_too_few_failures_is_refused(self):
        df = _lims(n=300, in_flight=10)
        df.loc[df["in_spec"] == "F", "in_spec"] = "T"       # wipe out the failures
        df.loc[df.index[:3], "in_spec"] = "F"               # leave 3
        with self.assertRaises(AnalyticsError) as ctx:
            predict_risk(df, "in_spec", positive_value="F")
        self.assertIn("too few", str(ctx.exception).lower())

    def test_a_single_outcome_is_refused(self):
        df = _lims(n=300, in_flight=10)
        df.loc[df["in_spec"].notna(), "in_spec"] = "T"
        with self.assertRaises(AnalyticsError) as ctx:
            predict_risk(df, "in_spec")
        self.assertIn("only one distinct outcome", str(ctx.exception))

    def test_a_dataset_with_no_in_flight_samples_still_reports_the_model(self):
        df = _lims(n=300, in_flight=0)
        out = predict_risk(df, "in_spec", positive_value="F", time_column="login_date")
        self.assertEqual(out["n_in_flight"], 0)
        self.assertEqual(out["at_risk"], [])
        self.assertIn("no in-flight samples", out["interpretation"])


class TatBreachTests(SimpleTestCase):
    """The same engine, pointed at turnaround: 'will this sample miss its SLA?'"""

    def test_it_predicts_an_sla_breach(self):
        rng = np.random.default_rng(11)
        n = 400
        priority = rng.choice(["ROUTINE", "URGENT"], n, p=[0.7, 0.3])
        backlog = rng.integers(0, 60, n)
        # Late when the lab is busy and the job isn't urgent.
        p_late = 0.05 + 0.010 * backlog + 0.15 * (priority == "ROUTINE")
        late = rng.random(n) < np.clip(p_late, 0, 0.95)

        df = pd.DataFrame({
            "login_date": pd.date_range("2026-01-01", periods=n, freq="4h"),
            "priority": priority,
            "backlog_at_login": backlog,
            "on_time": np.where(late, "N", "Y"),
        })
        df.loc[df.index[-30:], "on_time"] = None            # still in the lab

        out = predict_risk(df, "on_time", positive_value="N", time_column="login_date")
        self.assertTrue(out["usable"])
        self.assertGreater(out["metrics"]["roc_auc"], 0.65)
        top = [f["feature"] for f in out["feature_importance"][:3]]
        self.assertTrue(any("backlog" in f or "priority" in f for f in top), top)
