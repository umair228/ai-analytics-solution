"""Statistical Process Control — Westgard, Levey-Jennings, and the chart family.

DIS shipped one chart (I-MR) with two rules; "Westgard" appeared nowhere in the
codebase. SPC is exactly the kind of code that looks right and is wrong, so these
tests plant a known signal and assert that the specific rule which should catch it
does — and, just as importantly, that the rules which should NOT fire stay quiet.
A control scheme that cries wolf gets switched off.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from analytics import spc
from analytics.engine import AnalyticsError
from analytics.stats_catalog import run_test


def _df(values, **cols):
    return pd.DataFrame({"v": values, **cols})


def _rules_fired(out):
    return {v["rule"] for v in out["violations"]}


class ConstantsTests(SimpleTestCase):
    def test_d2_matches_the_standard_table(self):
        self.assertAlmostEqual(spc.d2(2), 1.128, places=3)
        self.assertAlmostEqual(spc.d2(5), 2.326, places=3)

    def test_c4_matches_the_standard_table(self):
        # c4 has a closed form, so it is computed rather than tabulated — which is
        # only safe if it reproduces the published values.
        self.assertAlmostEqual(spc.c4(2), 0.7979, places=4)
        self.assertAlmostEqual(spc.c4(5), 0.9400, places=4)
        self.assertAlmostEqual(spc.c4(10), 0.9727, places=4)


class WestgardRuleTests(SimpleTestCase):
    """Each rule gets a series built to trip exactly it."""

    MEAN, SD = 100.0, 2.0

    def _lj(self, values, rules="westgard"):
        return spc.levey_jennings(_df(values), "v", mean=self.MEAN, sd=self.SD,
                                  rules=rules)

    def test_1_3s_fires_on_a_single_wild_point(self):
        vals = [100, 101, 99, 100, 101, 99, 100, 108]      # +4 SD
        out = self._lj(vals)
        self.assertIn("1_3s", _rules_fired(out))
        self.assertFalse(out["in_control"])
        self.assertIn("random", out["error_types"])

    def test_2_2s_fires_on_two_consecutive_beyond_the_same_2sd(self):
        vals = [100, 100, 100, 100, 100, 105, 105.2]       # both ≈ +2.5 SD
        out = self._lj(vals)
        self.assertIn("2_2s", _rules_fired(out))
        # This is bias, not noise — and saying which is the whole point of the rule.
        self.assertIn("systematic", out["error_types"])

    def test_2_2s_does_not_fire_on_opposite_sides(self):
        """Two points beyond 2SD on OPPOSITE sides is R₄ₛ (imprecision), not 2₂ₛ
        (bias). Confusing them sends the analyst to fix the wrong thing."""
        vals = [100, 100, 100, 100, 100, 105, 95]
        fired = _rules_fired(self._lj(vals))
        self.assertNotIn("2_2s", fired)
        self.assertIn("R_4s", fired)

    def test_R_4s_fires_on_a_4sd_swing(self):
        vals = [100, 100, 100, 100, 100, 105, 94.9]
        out = self._lj(vals)
        self.assertIn("R_4s", _rules_fired(out))
        self.assertIn("random", out["error_types"])

    def test_4_1s_fires_on_four_consecutive_beyond_1sd_same_side(self):
        vals = [100, 100, 100, 103, 103, 103, 103]         # four at +1.5 SD
        out = self._lj(vals)
        self.assertIn("4_1s", _rules_fired(out))
        self.assertIn("systematic", out["error_types"])

    def test_4_1s_does_not_fire_when_the_side_flips(self):
        vals = [100, 100, 100, 103, 103, 97, 103]
        self.assertNotIn("4_1s", _rules_fired(self._lj(vals)))

    def test_10x_fires_on_ten_consecutive_on_one_side(self):
        vals = [100] + [100.5] * 10                        # tiny, but all one side
        out = self._lj(vals)
        self.assertIn("10_x", _rules_fired(out))
        # None of them is anywhere near a limit — which is exactly why 10ₓ exists.
        self.assertNotIn("1_3s", _rules_fired(out))
        self.assertIn("systematic", out["error_types"])

    def test_1_2s_is_a_WARNING_not_a_rejection(self):
        """1₂ₛ alone must not reject. Treating it as a rejection is the classic way
        to make a QC scheme so noisy that people start ignoring it."""
        vals = [100, 100, 100, 100, 100, 104.5]            # one point at +2.25 SD
        out = self._lj(vals)
        self.assertIn("1_2s", _rules_fired(out))
        self.assertEqual(out["reject_count"], 0)
        self.assertEqual(out["warning_count"], 1)
        self.assertTrue(out["in_control"])                 # a warning is not a failure

    def test_a_stable_run_fires_nothing(self):
        rng = np.random.default_rng(11)
        vals = list(self.MEAN + rng.normal(0, self.SD * 0.4, 30))
        out = self._lj(vals)
        self.assertTrue(out["in_control"], out["violations_by_rule"])
        self.assertEqual(out["reject_count"], 0)


class LeveyJenningsTests(SimpleTestCase):
    def test_established_limits_detect_a_shift(self):
        """The point of the whole module: a run centred 2SD high must be caught."""
        out = spc.levey_jennings(_df([104, 104.5, 103.8, 104.2, 104.1, 103.9]),
                                 "v", mean=100.0, sd=2.0)
        self.assertFalse(out["in_control"])
        self.assertTrue(out["established_limits"])
        self.assertIn("systematic", out["error_types"])

    def test_estimated_limits_are_blind_to_that_same_shift(self):
        """And the reason we shout about it: fit the limits to the shifted data and
        the chart re-centres on the shift, reporting 'in control' while the method
        drifts. This test exists to make that failure mode visible, not to bless it."""
        shifted = [104, 104.5, 103.8, 104.2, 104.1, 103.9]
        out = spc.levey_jennings(_df(shifted), "v")          # no mean/sd supplied
        self.assertTrue(out["in_control"])                   # <- the trap, demonstrated
        self.assertFalse(out["established_limits"])
        self.assertIn("CHARACTERISATION", out["warning"])
        self.assertIn("Characterisation only", out["interpretation"])

    def test_zero_sd_is_rejected_rather_than_dividing_by_zero(self):
        with self.assertRaises(AnalyticsError):
            spc.levey_jennings(_df([1, 2, 3, 4, 5]), "v", mean=1.0, sd=0.0)


class IMRTests(SimpleTestCase):
    def test_sigma_comes_from_the_moving_range_not_the_overall_sd(self):
        """A sustained shift inflates the overall SD, which widens the limits, which
        hides the shift that caused it. MR-bar/d2 is immune to that."""
        vals = [10] * 10 + [20] * 10                       # a step change
        out = spc.i_mr_chart(_df(vals), "v", rules="basic")
        overall_sd = float(np.std(vals, ddof=1))           # ≈ 5.1 — huge
        self.assertLess(out["sigma"], overall_sd / 2)
        self.assertFalse(out["in_control"])                # the step is caught

    def test_chart_exposes_the_sigma_zones(self):
        out = spc.i_mr_chart(_df(list(np.linspace(9, 11, 20))), "v")
        lim = out["limits"]
        self.assertLess(lim["lcl"], lim["lwl"])
        self.assertLess(lim["lwl"], lim["l1s"])
        self.assertLess(lim["u1s"], lim["uwl"])
        self.assertLess(lim["uwl"], lim["ucl"])


class NelsonRuleTests(SimpleTestCase):
    def test_trend_of_six_is_caught_though_no_point_is_out_of_limits(self):
        rng = np.random.default_rng(3)
        base = list(100 + rng.normal(0, 1, 20))
        trend = [100 + 0.4 * i for i in range(1, 8)]       # gentle, steady rise
        out = spc.i_mr_chart(_df(base + trend), "v", rules="nelson")
        self.assertIn("trend_6", _rules_fired(out))

    def test_hugging_the_centre_line_is_caught(self):
        # 15 points inside 1σ isn't "good", it's suspicious — usually over-averaged
        # or fabricated data.
        vals = [10, 14, 6, 10] + [10.0] * 16
        out = spc.i_mr_chart(_df(vals), "v", rules="nelson")
        self.assertIn("hugging_15", _rules_fired(out))

    def test_a_clean_process_fires_nothing(self):
        rng = np.random.default_rng(5)
        out = spc.i_mr_chart(_df(list(100 + rng.normal(0, 1, 40))), "v", rules="nelson")
        self.assertTrue(out["in_control"], out["violations_by_rule"])


class SubgroupChartTests(SimpleTestCase):
    def test_xbar_r_flags_a_drifting_batch(self):
        rng = np.random.default_rng(9)
        vals = list(rng.normal(50, 1, 100)) + list(rng.normal(56, 1, 5))
        out = spc.xbar_r_chart(_df(vals), "v", subgroup_size=5, rules="basic")
        self.assertEqual(out["subgroup_size"], 5)
        self.assertFalse(out["in_control"])
        self.assertIn("r_chart", out)

    def test_xbar_r_warns_when_the_R_chart_is_unstable(self):
        """The R chart gates the X̄ chart: if within-subgroup spread is unstable, the
        X̄ limits were computed from a variation estimate that is itself unstable."""
        rng = np.random.default_rng(4)
        vals = list(rng.normal(50, 0.5, 60)) + [50, 90, 10, 50, 50]   # one wild subgroup
        out = spc.xbar_r_chart(_df(vals), "v", subgroup_size=5, rules="basic")
        self.assertGreater(out["r_chart"].get("out_of_control", 0), 0)
        self.assertIn("R chart", out["interpretation"])

    def test_unequal_subgroups_are_rejected_not_silently_mislimited(self):
        df = pd.DataFrame({"v": [1, 2, 3, 4, 5, 6, 7],
                           "batch": ["a", "a", "a", "b", "b", "c", "c"]})
        with self.assertRaises(AnalyticsError) as ctx:
            spc.xbar_r_chart(df, "v", subgroup_column="batch")
        self.assertIn("unequal sizes", str(ctx.exception))

    def test_xbar_s_uses_c4(self):
        rng = np.random.default_rng(2)
        out = spc.xbar_s_chart(_df(list(rng.normal(20, 2, 120))), "v", subgroup_size=12)
        self.assertAlmostEqual(out["c4"], spc.c4(12), places=4)
        self.assertIn("s_chart", out)


class DriftChartTests(SimpleTestCase):
    """EWMA and CUSUM exist to catch what Shewhart cannot: a small sustained shift.

    A 1σ shift is how a degrading reagent lot or a slowly de-calibrating instrument
    presents. Shewhart's average run length for it is ~44 points; EWMA's is ~10.
    """

    # The in-control parameters, as you would have them from a characterisation run.
    MEAN, SD = 100.0, 1.0

    def _small_shift(self):
        rng = np.random.default_rng(17)
        stable = list(self.MEAN + rng.normal(0, self.SD, 30))
        drift = list(101.0 + rng.normal(0, self.SD, 25))   # a 1σ shift
        return stable + drift

    def test_shewhart_misses_a_1_sigma_shift(self):
        out = spc.i_mr_chart(_df(self._small_shift()), "v", rules="basic")
        self.assertTrue(out["in_control"])            # not one point breaches 3σ

    def test_ewma_catches_it(self):
        out = spc.ewma_chart(_df(self._small_shift()), "v",
                             mean=self.MEAN, sd=self.SD)
        self.assertFalse(out["in_control"])
        self.assertIn("systematic", out["error_types"])
        self.assertTrue(out["established_limits"])

    def test_cusum_catches_it(self):
        out = spc.cusum_chart(_df(self._small_shift()), "v",
                              target=self.MEAN, sd=self.SD)
        self.assertFalse(out["in_control"])

    def test_the_default_lambda_L_pair_can_actually_see_a_1_sigma_shift(self):
        """λ=0.2, L=3.0 — the pairing usually quoted — puts the asymptotic limit at
        exactly μ ± 1.0σ, so a 1σ shift lands ON the limit and is only caught when
        noise helps. That is a poor default for the one thing EWMA is for, hence
        λ=0.1, L=2.7 (Montgomery)."""
        vals = self._small_shift()
        naive = spc.ewma_chart(_df(vals), "v", lam=0.2, L=3.0,
                               mean=self.MEAN, sd=self.SD)
        default = spc.ewma_chart(_df(vals), "v", mean=self.MEAN, sd=self.SD)
        self.assertTrue(naive["in_control"])        # the quoted pairing misses it
        self.assertFalse(default["in_control"])     # ours does not

    def test_an_estimated_target_drags_the_centre_line_toward_the_shift(self):
        """The Levey-Jennings trap, in a second place — though milder here.

        Estimating the target from data that already contains the shift pulls the
        centre line toward it, so the chart measures the shift against a baseline the
        shift itself moved. EWMA survives it better than an LJ chart does, because its
        sigma comes from the moving range (robust to a step) rather than the sample
        SD — but the centre line is still wrong, and it is wrong in the direction that
        under-reports. Hence the warning; the fix is to supply the in-control mean.
        """
        out = spc.ewma_chart(_df(self._small_shift()), "v")   # no target supplied
        self.assertFalse(out["established_limits"])
        self.assertIn("already contain", out["warning"])
        self.assertIn("Target estimated", out["interpretation"])
        # The true in-control mean is 100.0; the contaminated estimate is above it.
        self.assertGreater(out["center_line"], 100.15)

    def test_supplying_the_target_puts_the_centre_line_where_it_belongs(self):
        out = spc.ewma_chart(_df(self._small_shift()), "v",
                             mean=self.MEAN, sd=self.SD)
        self.assertEqual(out["center_line"], 100.0)
        self.assertTrue(out["established_limits"])
        self.assertNotIn("warning", out)

    def test_neither_cries_wolf_on_a_stable_process(self):
        rng = np.random.default_rng(23)
        stable = list(self.MEAN + rng.normal(0, self.SD, 60))
        self.assertTrue(spc.ewma_chart(_df(stable), "v",
                                       mean=self.MEAN, sd=self.SD)["in_control"])
        self.assertTrue(spc.cusum_chart(_df(stable), "v",
                                        target=self.MEAN, sd=self.SD)["in_control"])


class AttributeChartTests(SimpleTestCase):
    def test_p_chart_uses_per_point_limits_when_subgroup_size_varies(self):
        """A single UCL would be a lie for every subgroup that isn't the average
        size — a small batch legitimately has wider limits."""
        df = pd.DataFrame({
            "off_spec": [2, 3, 1, 2, 2, 9],
            "tested":   [100, 120, 90, 110, 100, 40],   # last day is a small batch
        })
        out = spc.attribute_chart(df, chart="p", count_column="off_spec",
                                  size_column="tested")
        self.assertTrue(out["variable_limits"])
        ucls = {p["ucl"] for p in out["points"]}
        self.assertGreater(len(ucls), 1)
        self.assertFalse(out["in_control"])            # 22.5% on the last day

    def test_c_chart_on_defect_counts(self):
        df = pd.DataFrame({"defects": [3, 4, 2, 3, 5, 4, 3, 20]})
        out = spc.attribute_chart(df, chart="c", count_column="defects")
        self.assertFalse(out["in_control"])

    def test_np_chart_requires_a_constant_subgroup_size(self):
        df = pd.DataFrame({"d": [1, 2, 1, 2, 1], "n": [10, 10, 10, 20, 10]})
        with self.assertRaises(AnalyticsError) as ctx:
            spc.attribute_chart(df, chart="np", count_column="d", size_column="n")
        self.assertIn("CONSTANT", str(ctx.exception))


class CatalogWiringTests(SimpleTestCase):
    """The catalog drives BOTH the REST API and the agent tool, and the frontend
    form renders itself from it — so a test registered wrong is a feature nobody
    can reach."""

    def test_new_spc_tests_are_registered(self):
        from analytics.stats_catalog import TESTS
        for key in ("levey_jennings", "control_chart", "control_chart_xbar_r",
                    "control_chart_xbar_s", "control_chart_ewma",
                    "control_chart_cusum", "control_chart_attribute"):
            self.assertIn(key, TESTS)
            self.assertEqual(TESTS[key]["category"], "Quality")

    def test_levey_jennings_runs_through_the_dispatcher(self):
        out = run_test("levey_jennings", _df([100, 101, 99, 100, 108, 100]),
                       {"value_column": "v", "mean": 100, "sd": 2, "rules": "westgard"})
        self.assertEqual(out["test"], "levey_jennings")
        self.assertIn("1_3s", _rules_fired(out))

    def test_the_old_control_chart_key_still_works(self):
        """Existing dashboards and saved runs call `control_chart` — it must keep
        answering, just with a real rule engine behind it now."""
        out = run_test("control_chart", _df(list(np.linspace(10, 11, 20))),
                       {"value_column": "v"})
        self.assertEqual(out["test"], "control_chart_i_mr")
        self.assertIn("center_line", out)
        self.assertIn("violations", out)

    def test_every_rule_set_is_selectable_from_the_catalog(self):
        from analytics.stats_catalog import TESTS
        rules_param = next(p for p in TESTS["levey_jennings"]["params"]
                           if p["name"] == "rules")
        for option in rules_param["options"]:
            self.assertIn(option, spc.RULE_SETS)
