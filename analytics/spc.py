"""Statistical Process Control — the charts a QC laboratory actually runs on.

DIS shipped exactly one chart (I-MR) with two rules. A quality-control lab runs on
**Levey-Jennings charts** and **Westgard multirules**, and the string "Westgard" did
not appear anywhere in this codebase. That is not a missing nicety: it is the daily
instrument of QC, the thing a QC chemist opens every morning to decide whether the
run is releasable. Its absence is the difference between an analytics product and a
lab product.

This module provides:

* **Rule engines** — Westgard (1₂ₛ, 1₃ₛ, 2₂ₛ, R₄ₛ, 4₁ₛ, 10ₓ), the eight Nelson rules,
  and the four Western Electric zone rules. Each violation says *which* rule fired,
  over which points, and — for Westgard — whether it indicates **random** or
  **systematic** error, because that is what tells an analyst where to look.
* **Variables charts** — I-MR, X̄-R, X̄-S, EWMA and CUSUM. EWMA and CUSUM exist
  because a Shewhart chart is nearly blind to a small sustained shift (~1σ), which
  is exactly how a drifting instrument or a degrading reagent presents.
* **Attribute charts** — p, np, c, u. A p-chart of "proportion of results off-spec
  per day" is one of the most useful charts a lab can run and needs no new data.
* **Levey-Jennings** — an individuals chart plotted against the control material's
  **established** mean and SD.

⚠️ The single most important thing in this module: a Levey-Jennings chart must be
plotted against the mean/SD *assigned to the control material* (its certificate, or
a characterisation run), **not** the mean/SD of the data in front of you. Estimating
from the current data re-centres the chart on whatever shift has occurred, so the
shift becomes invisible — the chart cheerfully reports "in control" while the method
drifts. When ``mean``/``sd`` are not supplied we compute them, but we say so loudly
and mark the result as characterisation, not QC.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import AnalyticsError, _num

# --------------------------------------------------------------------------
# Shewhart constants. d2/d3 are expected values of the relative range and have
# no closed form — these are the standard tables. c4 does have one, so it is
# computed rather than tabulated.
# --------------------------------------------------------------------------
_D2 = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704, 8: 2.847,
       9: 2.970, 10: 3.078, 11: 3.173, 12: 3.258, 13: 3.336, 14: 3.407,
       15: 3.472, 16: 3.532, 17: 3.588, 18: 3.640, 19: 3.689, 20: 3.735,
       21: 3.778, 22: 3.819, 23: 3.858, 24: 3.895, 25: 3.931}
_D3 = {2: 0.853, 3: 0.888, 4: 0.880, 5: 0.864, 6: 0.848, 7: 0.833, 8: 0.820,
       9: 0.808, 10: 0.797, 11: 0.787, 12: 0.778, 13: 0.770, 14: 0.763,
       15: 0.756, 16: 0.750, 17: 0.744, 18: 0.739, 19: 0.734, 20: 0.729,
       21: 0.724, 22: 0.720, 23: 0.716, 24: 0.712, 25: 0.708}

MIN_POINTS = 5


def d2(n: int) -> float:
    if n not in _D2:
        raise AnalyticsError(f"Subgroup size {n} is out of range (2–25).")
    return _D2[n]


def c4(n: int) -> float:
    """E[S]/σ for a normal sample of size n — the unbiasing constant for X̄-S."""
    from scipy.special import gammaln
    if n < 2:
        raise AnalyticsError("Subgroup size must be at least 2.")
    return float(np.exp(0.5 * np.log(2.0 / (n - 1))
                        + gammaln(n / 2.0) - gammaln((n - 1) / 2.0)))


# --------------------------------------------------------------------------
# Rule engine
# --------------------------------------------------------------------------
# severity: "reject" stops the run; "warning" only prompts you to inspect the
# other rules (1₂ₛ alone is NOT a rejection — treating it as one is the classic
# way to make a QC scheme so noisy that people start ignoring it).
_RULE_META = {
    # Westgard
    "1_2s":   ("1₂ₛ", "warning", "One point beyond ±2SD — inspect the other rules.", ""),
    "1_3s":   ("1₃ₛ", "reject", "One point beyond ±3SD.", "random"),
    "2_2s":   ("2₂ₛ", "reject", "Two consecutive points beyond the same ±2SD limit.", "systematic"),
    "R_4s":   ("R₄ₛ", "reject", "Consecutive points span more than 4SD (one above +2SD, one below −2SD).", "random"),
    "4_1s":   ("4₁ₛ", "reject", "Four consecutive points beyond the same ±1SD limit.", "systematic"),
    "10_x":   ("10ₓ", "reject", "Ten consecutive points on the same side of the mean.", "systematic"),
    "2of3_2s": ("2of3₂ₛ", "reject", "Two of three consecutive points beyond the same ±2SD limit.", "systematic"),
    "7_T":    ("7T", "reject", "Seven consecutive points trending in one direction.", "systematic"),
    # Nelson / Western Electric
    "beyond_3s":     ("Beyond 3σ", "reject", "A point lies outside the 3σ control limits.", "random"),
    "run_9_side":    ("Run of 9", "reject", "Nine consecutive points on the same side of the centre line.", "systematic"),
    "trend_6":       ("Trend of 6", "reject", "Six consecutive points steadily increasing or decreasing.", "systematic"),
    "alternating_14": ("Alternating 14", "reject", "Fourteen consecutive points alternating up and down.", "systematic"),
    "2of3_2s_side":  ("2 of 3 beyond 2σ", "reject", "Two of three consecutive points beyond 2σ on the same side.", "systematic"),
    "4of5_1s_side":  ("4 of 5 beyond 1σ", "reject", "Four of five consecutive points beyond 1σ on the same side.", "systematic"),
    "hugging_15":    ("Hugging (15)", "reject", "Fifteen consecutive points within 1σ — variation is suspiciously small.", "systematic"),
    "avoid_8":       ("Avoiding centre (8)", "reject", "Eight consecutive points beyond 1σ on either side.", "systematic"),
    "run_8_side":    ("Run of 8", "reject", "Eight consecutive points on the same side of the centre line.", "systematic"),
}

RULE_SETS = {
    "basic": ["beyond_3s"],
    "westgard": ["1_2s", "1_3s", "2_2s", "R_4s", "4_1s", "10_x"],
    "westgard_extended": ["1_2s", "1_3s", "2_2s", "R_4s", "4_1s", "10_x", "2of3_2s", "7_T"],
    "western_electric": ["beyond_3s", "2of3_2s_side", "4of5_1s_side", "run_8_side"],
    "nelson": ["beyond_3s", "run_9_side", "trend_6", "alternating_14",
               "2of3_2s_side", "4of5_1s_side", "hugging_15", "avoid_8"],
}


def _side(z, i):
    return 1 if z[i] > 0 else -1 if z[i] < 0 else 0


def _k_of_m_same_side(z, i, k, m, limit):
    """k of the last m points beyond ``limit`` sigma, all on the same side."""
    if i + 1 < m:
        return False
    window = z[i - m + 1:i + 1]
    for sign in (1, -1):
        hits = [v for v in window if sign * v > limit]
        # The rule only fires on the completing point, so the last point must be
        # one of the hits — otherwise it re-flags a pattern already reported.
        if len(hits) >= k and sign * z[i] > limit:
            return True
    return False


def _run_same_side(z, i, k):
    if i + 1 < k:
        return False
    window = z[i - k + 1:i + 1]
    return all(v > 0 for v in window) or all(v < 0 for v in window)


def _evaluate(z, rules):
    """Apply a rule set to standardised points. Returns [{index, rule, ...}]."""
    n = len(z)
    out = []

    def flag(i, key):
        label, severity, desc, err = _RULE_META[key]
        out.append({"index": int(i), "rule": key, "rule_label": label,
                    "severity": severity, "description": desc, "error_type": err})

    for i in range(n):
        for key in rules:
            hit = False
            if key in ("1_3s", "beyond_3s"):
                hit = abs(z[i]) > 3
            elif key == "1_2s":
                hit = abs(z[i]) > 2
            elif key == "2_2s":
                hit = (i >= 1 and abs(z[i]) > 2 and abs(z[i - 1]) > 2
                       and _side(z, i) == _side(z, i - 1))
            elif key == "R_4s":
                # A 4SD swing between consecutive controls: one above +2SD and the
                # other below −2SD. That is imprecision, not bias — a different fix.
                hit = (i >= 1 and max(z[i], z[i - 1]) > 2 and min(z[i], z[i - 1]) < -2)
            elif key == "4_1s":
                hit = (i >= 3 and all(abs(v) > 1 for v in z[i - 3:i + 1])
                       and len({1 if v > 0 else -1 for v in z[i - 3:i + 1]}) == 1)
            elif key == "3_1s":
                hit = (i >= 2 and all(abs(v) > 1 for v in z[i - 2:i + 1])
                       and len({1 if v > 0 else -1 for v in z[i - 2:i + 1]}) == 1)
            elif key == "10_x":
                hit = _run_same_side(z, i, 10)
            elif key == "run_9_side":
                hit = _run_same_side(z, i, 9)
            elif key == "run_8_side":
                hit = _run_same_side(z, i, 8)
            elif key in ("2of3_2s", "2of3_2s_side"):
                hit = _k_of_m_same_side(z, i, 2, 3, 2)
            elif key == "4of5_1s_side":
                hit = _k_of_m_same_side(z, i, 4, 5, 1)
            elif key in ("trend_6", "7_T"):
                k = 6 if key == "trend_6" else 7
                if i + 1 >= k:
                    w = z[i - k + 1:i + 1]
                    d = np.diff(w)
                    hit = bool(np.all(d > 0) or np.all(d < 0))
            elif key == "alternating_14":
                if i + 1 >= 14:
                    d = np.diff(z[i - 13:i + 1])
                    hit = bool(np.all(d[:-1] * d[1:] < 0)) and bool(np.all(d != 0))
            elif key == "hugging_15":
                hit = i + 1 >= 15 and all(abs(v) < 1 for v in z[i - 14:i + 1])
            elif key == "avoid_8":
                hit = i + 1 >= 8 and all(abs(v) > 1 for v in z[i - 7:i + 1])
            if hit:
                flag(i, key)
    return out


# --------------------------------------------------------------------------
# Shared assembly
# --------------------------------------------------------------------------
def _labels(df, index_column, n):
    if index_column and index_column in df.columns:
        return [str(v) for v in df[index_column].astype(str).tolist()[:n]]
    return [str(i + 1) for i in range(n)]


def _numeric(df, column):
    if not column or column not in df.columns:
        raise AnalyticsError(f"Column '{column}' not found in the dataset.")
    s = pd.to_numeric(df[column], errors="coerce").dropna()
    if s.empty:
        raise AnalyticsError(f"Column '{column}' has no numeric values.")
    return s.to_numpy(dtype=float)


def _summarise(violations, rules_used):
    rejects = [v for v in violations if v["severity"] == "reject"]
    warnings = [v for v in violations if v["severity"] == "warning"]
    by_rule = {}
    for v in violations:
        by_rule[v["rule_label"]] = by_rule.get(v["rule_label"], 0) + 1
    errors = {v["error_type"] for v in rejects if v["error_type"]}
    return {
        "in_control": not rejects,
        "reject_count": len(rejects),
        "warning_count": len(warnings),
        "violations_by_rule": by_rule,
        "error_types": sorted(errors),
        "rules_applied": rules_used,
    }


def _assemble(*, test, y, cl, sigma, labels, rules, extra=None, value_column=""):
    if sigma is None or not np.isfinite(sigma) or sigma <= 0:
        raise AnalyticsError(
            "Cannot build a control chart: the process has zero variation, so the "
            "control limits would be degenerate.")
    z = (np.asarray(y, dtype=float) - cl) / sigma
    keys = RULE_SETS.get(rules)
    if keys is None:
        raise AnalyticsError(
            f"Unknown rule set '{rules}'. Available: {', '.join(RULE_SETS)}.")
    violations = _evaluate(z, keys)
    for v in violations:
        v["label"] = labels[v["index"]]
        v["value"] = _num(float(y[v["index"]]))
        v["sd_from_mean"] = _num(float(z[v["index"]]))

    summary = _summarise(violations, keys)
    points = [{"label": labels[i], "value": _num(float(y[i])),
               "sd_from_mean": _num(float(z[i]))} for i in range(len(y))][:1000]

    out = {
        "test": test,
        "value_column": value_column,
        "n": int(len(y)),
        "center_line": _num(float(cl)),
        "sigma": _num(float(sigma)),
        # Zone boundaries: a control chart without its ±1σ/±2σ zones cannot show
        # the zone rules that fired.
        "limits": {
            "ucl": _num(float(cl + 3 * sigma)), "lcl": _num(float(cl - 3 * sigma)),
            "uwl": _num(float(cl + 2 * sigma)), "lwl": _num(float(cl - 2 * sigma)),
            "u1s": _num(float(cl + sigma)), "l1s": _num(float(cl - sigma)),
        },
        "points": points,
        "violations": violations[:200],
        **summary,
    }
    out.update(extra or {})
    return out


def _interpret(out, subject):
    if out["in_control"]:
        return (f"{subject}: in control across {out['n']} points "
                f"({', '.join(out['rules_applied'])} applied, no rule violated).")
    rules = ", ".join(f"{k}×{v}" for k, v in out["violations_by_rule"].items())
    errs = out.get("error_types") or []
    hint = ""
    if "systematic" in errs and "random" in errs:
        hint = " Both systematic (bias) and random (imprecision) error are indicated."
    elif "systematic" in errs:
        hint = (" The pattern indicates SYSTEMATIC error — a shift or drift "
                "(calibration, reagent lot, a new instrument setting).")
    elif "random" in errs:
        hint = (" The pattern indicates RANDOM error — imprecision "
                "(bubbles, poor pipetting, an unstable instrument).")
    return (f"{subject}: OUT OF CONTROL — {out['reject_count']} rejection(s) "
            f"across {out['n']} points ({rules}).{hint}")


# --------------------------------------------------------------------------
# Levey-Jennings — the QC chart
# --------------------------------------------------------------------------
def levey_jennings(df, value_column, mean=None, sd=None, index_column=None,
                   rules="westgard", material=""):
    """QC control chart for a control material, with Westgard multirules.

    ``mean``/``sd`` are the values **assigned to the control material** — from its
    certificate, or from a characterisation run. Supply them. If you do not, we
    estimate them from the data you passed, and the chart silently re-centres on
    whatever shift has already happened: the method drifts and the chart reports
    "in control". We therefore mark that case as characterisation, not QC.
    """
    y = _numeric(df, value_column)
    if len(y) < MIN_POINTS:
        raise AnalyticsError(f"Need at least {MIN_POINTS} points for a QC chart.")

    established = mean is not None and sd is not None
    cl = float(mean) if mean is not None else float(np.mean(y))
    s = float(sd) if sd is not None else float(np.std(y, ddof=1))
    if s <= 0:
        raise AnalyticsError("The control SD must be greater than zero.")

    out = _assemble(
        test="levey_jennings", y=y, cl=cl, sigma=s,
        labels=_labels(df, index_column, len(y)), rules=rules,
        value_column=value_column,
        extra={
            "material": material or value_column,
            "established_limits": established,
            "target_mean": _num(cl),
            "target_sd": _num(s),
        },
    )

    subject = f"Levey-Jennings for {material or value_column}"
    out["interpretation"] = _interpret(out, subject)
    if not established:
        # Say it plainly, and keep saying it: this is the mistake that makes a QC
        # chart look reassuring precisely when it should not.
        out["warning"] = (
            "No established mean/SD was supplied, so both were estimated from these "
            "same points. That makes this a CHARACTERISATION chart, not a QC chart: "
            "limits fitted to the data cannot detect a shift the data already "
            "contains. Use the control material's assigned mean and SD for QC."
        )
        out["interpretation"] += " (Characterisation only — limits estimated from the data.)"
    return out


# --------------------------------------------------------------------------
# Variables charts
# --------------------------------------------------------------------------
def _subgroups(df, value_column, subgroup_column=None, subgroup_size=None):
    """[(label, values), ...] — either grouped by a column or chunked by size."""
    if subgroup_column and subgroup_column in df.columns:
        groups = []
        for key, chunk in df.groupby(subgroup_column, sort=False):
            vals = pd.to_numeric(chunk[value_column], errors="coerce").dropna().to_numpy(float)
            if len(vals) >= 2:
                groups.append((str(key), vals))
        if len(groups) < 2:
            raise AnalyticsError(
                f"Need at least 2 subgroups of 2+ values; grouping by "
                f"'{subgroup_column}' produced {len(groups)}.")
        sizes = {len(v) for _, v in groups}
        if len(sizes) > 1:
            # Unequal subgroups make the constants (A2/D3/D4) wrong per group.
            # Rather than silently mis-limit the chart, say so.
            raise AnalyticsError(
                f"Subgroups from '{subgroup_column}' have unequal sizes {sorted(sizes)}. "
                "X̄-R/X̄-S need a constant subgroup size — use a size-based subgroup, "
                "or an I-MR chart for individual values.")
        return groups

    n = int(subgroup_size or 5)
    if n < 2 or n > 25:
        raise AnalyticsError("subgroup_size must be between 2 and 25.")
    y = _numeric(df, value_column)
    k = len(y) // n
    if k < 2:
        raise AnalyticsError(f"Need at least {2 * n} values for subgroups of {n}.")
    return [(f"{i + 1}", y[i * n:(i + 1) * n]) for i in range(k)]


def i_mr_chart(df, value_column, index_column=None, rules="nelson"):
    """Individuals + moving-range chart, for one measurement per time point."""
    y = _numeric(df, value_column)
    if len(y) < MIN_POINTS:
        raise AnalyticsError(f"Need at least {MIN_POINTS} points for a control chart.")

    cl = float(np.mean(y))
    mr = np.abs(np.diff(y))
    mr_bar = float(np.mean(mr)) if len(mr) else 0.0
    # Sigma from the moving range, NOT the overall SD: a shift inflates the overall
    # SD, which widens the limits, which hides the shift that caused it.
    sigma = mr_bar / d2(2) if mr_bar > 0 else float(np.std(y, ddof=1))

    out = _assemble(test="control_chart_i_mr", y=y, cl=cl, sigma=sigma,
                    labels=_labels(df, index_column, len(y)), rules=rules,
                    value_column=value_column,
                    extra={"mr_bar": _num(mr_bar),
                           "mr_chart": {"center_line": _num(mr_bar),
                                        "ucl": _num(3.267 * mr_bar), "lcl": 0.0}})
    out["interpretation"] = _interpret(out, f"{value_column} I-chart")
    return out


def xbar_r_chart(df, value_column, subgroup_column=None, subgroup_size=5, rules="nelson"):
    """X̄-R — subgroup means, with sigma estimated from the average range."""
    groups = _subgroups(df, value_column, subgroup_column, subgroup_size)
    n = len(groups[0][1])
    if n > 10:
        raise AnalyticsError("X̄-R is for subgroups of 10 or fewer — use X̄-S above that.")

    labels = [g[0] for g in groups]
    means = np.array([float(np.mean(v)) for _, v in groups])
    ranges = np.array([float(np.max(v) - np.min(v)) for _, v in groups])
    grand, r_bar = float(np.mean(means)), float(np.mean(ranges))
    sigma_within = r_bar / d2(n) if r_bar > 0 else float(np.std(means, ddof=1))
    sigma_xbar = sigma_within / np.sqrt(n)

    d3_, d2_ = _D3[n], d2(n)
    r_ucl = r_bar * (1 + 3 * d3_ / d2_)
    r_lcl = max(0.0, r_bar * (1 - 3 * d3_ / d2_))

    out = _assemble(test="control_chart_xbar_r", y=means, cl=grand, sigma=sigma_xbar,
                    labels=labels, rules=rules, value_column=value_column,
                    extra={
                        "subgroup_size": n, "subgroups": len(groups),
                        "sigma_within": _num(sigma_within), "r_bar": _num(r_bar),
                        "r_chart": {"center_line": _num(r_bar), "ucl": _num(r_ucl),
                                    "lcl": _num(r_lcl),
                                    "points": [{"label": labels[i],
                                                "value": _num(float(ranges[i]))}
                                               for i in range(len(ranges))][:1000]},
                    })
    out["interpretation"] = _interpret(
        out, f"{value_column} X̄ chart (subgroups of {n})")
    # The R chart gates the X̄ chart: if the within-subgroup spread is unstable, the
    # X̄ limits are computed from a variation estimate that itself is not stable.
    r_oc = int(np.sum((ranges > r_ucl) | (ranges < r_lcl)))
    if r_oc:
        out["r_chart"]["out_of_control"] = r_oc
        out["interpretation"] += (
            f" NOTE: the R chart has {r_oc} out-of-control point(s) — within-subgroup "
            "variation is unstable, so the X̄ limits above are unreliable. Fix the "
            "spread before reading the mean.")
    return out


def xbar_s_chart(df, value_column, subgroup_column=None, subgroup_size=10, rules="nelson"):
    """X̄-S — subgroup means with sigma from the average subgroup SD (large n)."""
    groups = _subgroups(df, value_column, subgroup_column, subgroup_size)
    n = len(groups[0][1])
    labels = [g[0] for g in groups]
    means = np.array([float(np.mean(v)) for _, v in groups])
    sds = np.array([float(np.std(v, ddof=1)) for _, v in groups])
    grand, s_bar = float(np.mean(means)), float(np.mean(sds))
    c4n = c4(n)
    sigma_within = s_bar / c4n if s_bar > 0 else float(np.std(means, ddof=1))
    sigma_xbar = sigma_within / np.sqrt(n)

    tail = 3.0 / (c4n * np.sqrt(2.0 * (n - 1)))
    s_ucl, s_lcl = s_bar * (1 + tail), max(0.0, s_bar * (1 - tail))

    out = _assemble(test="control_chart_xbar_s", y=means, cl=grand, sigma=sigma_xbar,
                    labels=labels, rules=rules, value_column=value_column,
                    extra={
                        "subgroup_size": n, "subgroups": len(groups),
                        "sigma_within": _num(sigma_within), "s_bar": _num(s_bar),
                        "c4": _num(c4n),
                        "s_chart": {"center_line": _num(s_bar), "ucl": _num(s_ucl),
                                    "lcl": _num(s_lcl),
                                    "points": [{"label": labels[i],
                                                "value": _num(float(sds[i]))}
                                               for i in range(len(sds))][:1000]},
                    })
    out["interpretation"] = _interpret(
        out, f"{value_column} X̄ chart (subgroups of {n}, S-based)")
    return out


_ESTIMATED_TARGET_WARNING = (
    "No in-control target was supplied, so it was estimated from these same points "
    "— which already contain whatever shift you are looking for. The centre line "
    "moves toward the shift and the chart under-reports it. Supply the in-control "
    "mean (and SD) from a characterisation run for a true monitoring chart."
)


def ewma_chart(df, value_column, lam=0.1, L=2.7, mean=None, sd=None,
               index_column=None):
    """EWMA — built to catch the small sustained shift a Shewhart chart misses.

    A Shewhart chart is nearly blind to a drift of about 1σ, which is exactly how a
    degrading reagent lot or a slowly de-calibrating instrument presents. EWMA
    accumulates the history, so a small persistent bias eventually breaches.

    Defaults are λ=0.1, L=2.7 (Montgomery), not the often-quoted λ=0.2, L=3.0. The
    latter puts the asymptotic limit at μ ± L·σ·√(λ/(2−λ)) = μ ± 1.0σ — so a 1σ
    shift lands exactly ON the limit and is only caught when noise happens to help.
    That is a poor default for the one thing this chart exists to detect.

    Like a Levey-Jennings chart, this is only a monitoring chart if ``mean``/``sd``
    describe the IN-CONTROL process. Estimated from contaminated data, it goes blind.
    """
    y = _numeric(df, value_column)
    if len(y) < MIN_POINTS:
        raise AnalyticsError(f"Need at least {MIN_POINTS} points for an EWMA chart.")
    lam = float(lam)
    if not 0 < lam <= 1:
        raise AnalyticsError("lambda must be in (0, 1].")

    estimated = mean is None
    mu = float(mean) if mean is not None else float(np.mean(y))
    mr_bar = float(np.mean(np.abs(np.diff(y))))
    s = float(sd) if sd is not None else (mr_bar / d2(2) if mr_bar > 0
                                          else float(np.std(y, ddof=1)))
    if s <= 0:
        raise AnalyticsError("Cannot build an EWMA chart with zero variation.")

    labels = _labels(df, index_column, len(y))
    z, prev = [], mu
    for v in y:
        prev = lam * float(v) + (1 - lam) * prev
        z.append(prev)
    z = np.array(z)

    i = np.arange(1, len(y) + 1)
    # The limits widen as the statistic warms up, then settle at the asymptote.
    sig_i = s * np.sqrt((lam / (2 - lam)) * (1 - (1 - lam) ** (2 * i)))
    ucl, lcl = mu + L * sig_i, mu - L * sig_i

    viol = [{"index": int(k), "label": labels[k], "value": _num(float(z[k])),
             "rule": "ewma_limit", "rule_label": f"EWMA ±{L}σ", "severity": "reject",
             "error_type": "systematic",
             "description": "The EWMA statistic crossed its control limit — a small "
                            "sustained shift the individual points may not show."}
            for k in range(len(z)) if z[k] > ucl[k] or z[k] < lcl[k]]

    out = {
        "test": "control_chart_ewma", "value_column": value_column, "n": int(len(y)),
        "lambda": _num(lam), "L": _num(L),
        "center_line": _num(mu), "sigma": _num(s),
        "established_limits": not estimated,
        "in_control": not viol, "reject_count": len(viol), "warning_count": 0,
        "violations": viol[:200],
        "violations_by_rule": {f"EWMA ±{L}σ": len(viol)} if viol else {},
        "error_types": ["systematic"] if viol else [],
        "rules_applied": ["ewma_limit"],
        "points": [{"label": labels[k], "value": _num(float(y[k])),
                    "ewma": _num(float(z[k])), "ucl": _num(float(ucl[k])),
                    "lcl": _num(float(lcl[k]))} for k in range(len(y))][:1000],
        "interpretation": (
            f"{value_column} EWMA (λ={lam}): in control."
            if not viol else
            f"{value_column} EWMA (λ={lam}): {len(viol)} point(s) beyond the limit — "
            "a small sustained shift is present even where individual points look fine."
        ),
    }
    if estimated:
        out["warning"] = _ESTIMATED_TARGET_WARNING
        out["interpretation"] += " (Target estimated from the data — see warning.)"
    return out


def cusum_chart(df, value_column, target=None, k=0.5, h=4.0, sd=None, index_column=None):
    """Tabular CUSUM — accumulates deviation from target; k=slack, h=decision limit.

    Detects a shift of about 2k σ fastest. The defaults (k=0.5, h=4) are tuned for a
    1σ shift, which is the one that matters and the one Shewhart misses.
    """
    y = _numeric(df, value_column)
    if len(y) < MIN_POINTS:
        raise AnalyticsError(f"Need at least {MIN_POINTS} points for a CUSUM chart.")

    estimated = target is None
    mu = float(target) if target is not None else float(np.mean(y))
    mr_bar = float(np.mean(np.abs(np.diff(y))))
    s = float(sd) if sd is not None else (mr_bar / d2(2) if mr_bar > 0
                                          else float(np.std(y, ddof=1)))
    if s <= 0:
        raise AnalyticsError("Cannot build a CUSUM chart with zero variation.")

    K, H = float(k) * s, float(h) * s
    labels = _labels(df, index_column, len(y))

    cp, cm, hi, lo = 0.0, 0.0, [], []
    for v in y:
        cp = max(0.0, float(v) - (mu + K) + cp)
        cm = max(0.0, (mu - K) - float(v) + cm)
        hi.append(cp)
        lo.append(cm)

    viol = []
    for i in range(len(y)):
        if hi[i] > H or lo[i] > H:
            up = hi[i] > H
            viol.append({
                "index": i, "label": labels[i], "value": _num(float(y[i])),
                "rule": "cusum_upper" if up else "cusum_lower",
                "rule_label": f"CUSUM {'C+' if up else 'C−'} > {h}σ",
                "severity": "reject", "error_type": "systematic",
                "description": ("The cumulative sum crossed the decision interval — a "
                                f"sustained {'upward' if up else 'downward'} shift from target."),
            })

    by_rule = {}
    for v in viol:
        by_rule[v["rule_label"]] = by_rule.get(v["rule_label"], 0) + 1

    out = {
        "test": "control_chart_cusum", "value_column": value_column, "n": int(len(y)),
        "target": _num(mu), "sigma": _num(s), "k": _num(k), "h": _num(h),
        "decision_interval": _num(H), "slack": _num(K),
        "established_limits": not estimated,
        "in_control": not viol, "reject_count": len(viol), "warning_count": 0,
        "violations": viol[:200], "violations_by_rule": by_rule,
        "error_types": ["systematic"] if viol else [],
        "rules_applied": ["cusum"],
        "points": [{"label": labels[i], "value": _num(float(y[i])),
                    "cusum_high": _num(float(hi[i])), "cusum_low": _num(float(lo[i]))}
                   for i in range(len(y))][:1000],
        "limits": {"h": _num(H)},
        "interpretation": (
            f"{value_column} CUSUM (k={k}, h={h}): in control — no sustained shift from target."
            if not viol else
            f"{value_column} CUSUM (k={k}, h={h}): {len(viol)} signal(s) — the process has "
            f"drifted away from the target of {mu:.4g} and stayed there."
        ),
    }
    if estimated:
        out["warning"] = _ESTIMATED_TARGET_WARNING
        out["interpretation"] += " (Target estimated from the data — see warning.)"
    return out


# --------------------------------------------------------------------------
# Attribute charts
# --------------------------------------------------------------------------
def attribute_chart(df, chart="p", count_column=None, size_column=None,
                    index_column=None, rules="basic"):
    """p / np / c / u charts — for counted data rather than measured data.

    A p-chart of "proportion of results off-spec per day" is one of the most useful
    charts a lab can run, and it needs no data the LIMS does not already have.
    """
    chart = (chart or "p").lower()
    if chart not in ("p", "np", "c", "u"):
        raise AnalyticsError("chart must be one of: p, np, c, u.")
    if not count_column:
        raise AnalyticsError("count_column is required.")

    counts = _numeric(df, count_column)
    labels = _labels(df, index_column, len(counts))
    if len(counts) < MIN_POINTS:
        raise AnalyticsError(f"Need at least {MIN_POINTS} points.")

    if chart in ("p", "u", "np"):
        if not size_column and chart != "np":
            raise AnalyticsError(f"size_column is required for a {chart}-chart.")
    sizes = _numeric(df, size_column) if size_column else None
    if sizes is not None:
        if len(sizes) != len(counts):
            raise AnalyticsError("count_column and size_column have different lengths.")
        if np.any(sizes <= 0):
            raise AnalyticsError("size_column must be positive.")

    if chart == "p":
        y = counts / sizes
        cl = float(np.sum(counts) / np.sum(sizes))
        sig = np.sqrt(cl * (1 - cl) / sizes)          # varies with subgroup size
        title = "proportion nonconforming"
    elif chart == "np":
        if sizes is None:
            raise AnalyticsError("size_column is required for an np-chart (constant n).")
        if len({round(float(v), 6) for v in sizes}) != 1:
            raise AnalyticsError("An np-chart needs a CONSTANT subgroup size — use a p-chart.")
        n = float(sizes[0])
        y = counts
        p_bar = float(np.sum(counts) / (n * len(counts)))
        cl = n * p_bar
        sig = np.full(len(counts), np.sqrt(n * p_bar * (1 - p_bar)))
        title = "number nonconforming"
    elif chart == "c":
        y = counts
        cl = float(np.mean(counts))
        sig = np.full(len(counts), np.sqrt(cl))
        title = "defect count"
    else:  # u
        y = counts / sizes
        cl = float(np.sum(counts) / np.sum(sizes))
        sig = np.sqrt(cl / sizes)
        title = "defects per unit"

    ucl = cl + 3 * sig
    lcl = np.maximum(0.0, cl - 3 * sig)

    viol = [{"index": i, "label": labels[i], "value": _num(float(y[i])),
             "rule": "beyond_3s", "rule_label": "Beyond 3σ", "severity": "reject",
             "error_type": "random",
             "description": "The point lies outside the 3σ control limits."}
            for i in range(len(y)) if y[i] > ucl[i] or y[i] < lcl[i]]

    return {
        "test": f"control_chart_{chart}", "value_column": count_column,
        "chart": chart, "n": int(len(y)),
        "center_line": _num(float(cl)),
        "in_control": not viol, "reject_count": len(viol), "warning_count": 0,
        "violations": viol[:200],
        "violations_by_rule": {"Beyond 3σ": len(viol)} if viol else {},
        "error_types": ["random"] if viol else [],
        "rules_applied": ["beyond_3s"],
        # Limits vary per point when the subgroup size does — a single UCL would be
        # a lie for every subgroup that isn't the average size.
        "points": [{"label": labels[i], "value": _num(float(y[i])),
                    "ucl": _num(float(ucl[i])), "lcl": _num(float(lcl[i]))}
                   for i in range(len(y))][:1000],
        "limits": {"ucl": _num(float(np.mean(ucl))), "lcl": _num(float(np.mean(lcl)))},
        "variable_limits": bool(sizes is not None and len(set(np.round(sizes, 6))) > 1),
        "interpretation": (
            f"{chart}-chart ({title}): in control across {len(y)} subgroups."
            if not viol else
            f"{chart}-chart ({title}): {len(viol)} subgroup(s) out of control."
        ),
    }
