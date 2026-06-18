"""Comprehensive statistical analysis engine for DIS.

Implements the MOM statistics requirements on top of scipy / statsmodels /
scikit-learn: descriptive stats, normality, t-/F-/chi-square tests, one-way
ANOVA (+ Tukey), correlation, regression (linear / polynomial / non-linear),
outlier & box-plot analysis, control charts and process capability, and trend
analysis.

Design: every function is DETERMINISTIC and returns a JSON-safe dict with the
statistic, the p-value, supporting numbers, and a plain-English ``interpretation``
string. The AI only narrates these results — the maths is exact and reproducible,
which is what a regulated QC lab needs. All numbers pass through ``engine._num``
(rounds + nulls NaN/inf).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import optimize, stats

from .engine import AnalyticsError, _num

# statsmodels is heavier; import lazily inside the functions that need it.


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _p(value):
    """Round a p-value/keep small ones readable; None on nan."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(v) or np.isinf(v):
        return None
    return round(v, 6)


def _numeric(df, column):
    if not column or column not in df.columns:
        raise AnalyticsError(f"Column '{column}' not found in the dataset.")
    s = pd.to_numeric(df[column], errors="coerce").dropna()
    if s.empty:
        raise AnalyticsError(f"Column '{column}' has no numeric values.")
    return s.to_numpy(dtype=float)


def _split_two(df, value_column, group_column):
    """Split a numeric column into exactly two groups by a category column."""
    if group_column not in df.columns:
        raise AnalyticsError(f"Group column '{group_column}' not found.")
    val = pd.to_numeric(df[value_column], errors="coerce")
    grp = df[group_column].astype(str)
    groups = {g: sub.dropna().to_numpy(float)
              for g, sub in val.groupby(grp) if sub.notna().sum() >= 2}
    if len(groups) != 2:
        raise AnalyticsError(
            f"Two-sample test needs exactly 2 groups with data; "
            f"'{group_column}' yielded {len(groups)}."
        )
    (n1, a), (n2, b) = groups.items()
    return (n1, a), (n2, b)


def _named_groups(df, value_column, group_column, min_n=2, max_groups=30):
    val = pd.to_numeric(df[value_column], errors="coerce")
    grp = df[group_column].astype(str)
    groups = {}
    for g, sub in val.groupby(grp):
        clean = sub.dropna().to_numpy(float)
        if len(clean) >= min_n:
            groups[g] = clean
    if len(groups) < 2:
        raise AnalyticsError(
            f"Need ≥2 groups with ≥{min_n} numeric values each in "
            f"'{group_column}'. Found {len(groups)}."
        )
    if len(groups) > max_groups:
        raise AnalyticsError(
            f"Too many groups ({len(groups)}) in '{group_column}'; "
            f"limit is {max_groups}. Filter or choose a coarser grouping."
        )
    return groups


def _sig(p, alpha):
    if p is None:
        return "could not be determined"
    return (f"statistically significant (p={_p(p)} < α={alpha})" if p < alpha
            else f"not statistically significant (p={_p(p)} ≥ α={alpha})")


def _corr_strength(r):
    a = abs(r)
    if a >= 0.8:
        s = "very strong"
    elif a >= 0.6:
        s = "strong"
    elif a >= 0.4:
        s = "moderate"
    elif a >= 0.2:
        s = "weak"
    else:
        s = "very weak / negligible"
    return f"{s} {'positive' if r >= 0 else 'negative'}"


def _ci_mean(arr, alpha=0.05):
    """Two-sided confidence interval for a mean (t-based). [lo, hi]."""
    arr = np.asarray(arr, dtype=float)
    n = len(arr)
    if n < 2:
        return [None, None]
    mean = float(np.mean(arr))
    sem = float(stats.sem(arr))
    if sem == 0 or np.isnan(sem):
        return [_num(mean), _num(mean)]
    lo, hi = stats.t.interval(1 - alpha, n - 1, loc=mean, scale=sem)
    return [_num(lo), _num(hi)]


def _cohens_d(a, b, paired=False):
    """Standardised mean difference (effect size)."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    if paired:
        diff = a - b
        sd = float(np.std(diff, ddof=1)) if len(diff) > 1 else 0.0
        return float(np.mean(diff) / sd) if sd else 0.0
    na, nb = len(a), len(b)
    pooled = ((na - 1) * np.var(a, ddof=1) + (nb - 1) * np.var(b, ddof=1)) / max(na + nb - 2, 1)
    sp = float(np.sqrt(pooled))
    return float((np.mean(a) - np.mean(b)) / sp) if sp else 0.0


def _d_magnitude(d):
    a = abs(d)
    return ("large" if a >= 0.8 else "medium" if a >= 0.5
            else "small" if a >= 0.2 else "negligible")


def _assume(groups: dict, alpha=0.05) -> dict:
    """Assumption checks for parametric comparisons.

    ``groups`` maps label -> 1-D array. Checks per-group normality (Shapiro,
    where 3≤n≤5000) and variance homogeneity (Levene, robust). Returns a dict
    with a parametric/non-parametric recommendation and advisory warnings.
    """
    normality, all_normal = {}, True
    for label, arr in groups.items():
        arr = np.asarray(arr, float)
        n = len(arr)
        ok = True
        if 3 <= n <= 5000:
            try:
                _, p = stats.shapiro(arr)
                ok = bool(p > alpha)
            except Exception:  # noqa: BLE001
                ok = True
        normality[str(label)] = ok
        all_normal = all_normal and ok

    equal_var, levene_p = True, None
    arrays = [np.asarray(a, float) for a in groups.values() if len(a) >= 2]
    if len(arrays) >= 2:
        try:
            _, lp = stats.levene(*arrays)
            levene_p = _p(lp)
            equal_var = bool(lp > alpha)
        except Exception:  # noqa: BLE001
            pass

    warnings = []
    if not all_normal:
        warnings.append(
            "One or more groups deviate from normality (Shapiro p<α) — consider a "
            "non-parametric test (Mann-Whitney / Kruskal-Wallis) or a transformation."
        )
    if not equal_var:
        warnings.append(
            "Group variances differ (Levene p<α) — prefer Welch's t-test and "
            "interpret ANOVA with caution."
        )
    return {
        "normality": normality,
        "all_normal": all_normal,
        "equal_variance": equal_var,
        "levene_p": levene_p,
        "recommended": "parametric" if all_normal else "nonparametric",
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Descriptive statistics
# --------------------------------------------------------------------------
def descriptive_stats(df, column):
    y = _numeric(df, column)
    n = len(y)
    mean = float(np.mean(y))
    std = float(np.std(y, ddof=1)) if n > 1 else 0.0
    q1, med, q3 = (float(v) for v in np.percentile(y, [25, 50, 75]))
    return {
        "test": "descriptive_statistics",
        "column": column,
        "n": n,
        "mean": _num(mean),
        "median": _num(med),
        "mode": _num(float(stats.mode(y, keepdims=False).mode)),
        "std": _num(std),
        "variance": _num(std ** 2),
        "cv_pct": _num(std / mean * 100) if mean else None,
        "min": _num(float(np.min(y))),
        "max": _num(float(np.max(y))),
        "range": _num(float(np.max(y) - np.min(y))),
        "q1": _num(q1), "q3": _num(q3), "iqr": _num(q3 - q1),
        "skewness": _num(float(stats.skew(y))),
        "kurtosis": _num(float(stats.kurtosis(y))),  # excess (normal=0)
        "sem": _num(std / np.sqrt(n)) if n else None,
        "interpretation": (
            f"{column}: n={n}, mean={mean:.4g}, median={med:.4g}, sd={std:.4g}. "
            f"Distribution is {'right' if stats.skew(y) > 0.5 else 'left' if stats.skew(y) < -0.5 else 'roughly symmetric'}-"
            f"skewed (skew={stats.skew(y):.2f})."
        ),
    }


# --------------------------------------------------------------------------
# Normality
# --------------------------------------------------------------------------
def normality_test(df, column, alpha=0.05):
    y = _numeric(df, column)
    n = len(y)
    if n < 3:
        raise AnalyticsError("Need at least 3 values for a normality test.")
    results = {}

    if n <= 5000:
        w, p = stats.shapiro(y)
        results["shapiro_wilk"] = {"statistic": _num(w), "p_value": _p(p),
                                   "normal": bool(p > alpha)}
    if n >= 8:
        k2, p = stats.normaltest(y)  # D'Agostino-Pearson
        results["dagostino_k2"] = {"statistic": _num(k2), "p_value": _p(p),
                                   "normal": bool(p > alpha)}
    ad = stats.anderson(y, dist="norm")
    # 5% critical value is index 2 in scipy's default significance levels.
    crit5 = float(ad.critical_values[2])
    results["anderson_darling"] = {
        "statistic": _num(float(ad.statistic)),
        "critical_value_5pct": _num(crit5),
        "normal": bool(ad.statistic < crit5),
    }

    verdicts = [r["normal"] for r in results.values()]
    is_normal = sum(verdicts) >= (len(verdicts) / 2)
    return {
        "test": "normality",
        "column": column,
        "n": n,
        "alpha": alpha,
        "results": results,
        "is_normal": is_normal,
        "interpretation": (
            f"{column} appears {'NORMALLY' if is_normal else 'NOT normally'} "
            f"distributed ({sum(verdicts)}/{len(verdicts)} tests agree). "
            + ("Parametric tests (t-test, ANOVA) are appropriate."
               if is_normal else
               "Consider non-parametric tests or a transformation.")
        ),
    }


# --------------------------------------------------------------------------
# t-tests
# --------------------------------------------------------------------------
def t_test_one_sample(df, column, popmean, alpha=0.05):
    if popmean is None:
        raise AnalyticsError("A 'popmean' (hypothesised mean) is required.")
    y = _numeric(df, column)
    popmean = float(popmean)
    t, p = stats.ttest_1samp(y, popmean)
    mean = float(np.mean(y))
    sem = float(stats.sem(y))
    ci = stats.t.interval(1 - alpha, len(y) - 1, loc=mean, scale=sem)
    return {
        "test": "one_sample_t_test",
        "column": column,
        "n": len(y),
        "sample_mean": _num(mean),
        "hypothesised_mean": _num(popmean),
        "t_statistic": _num(float(t)),
        "df": len(y) - 1,
        "p_value": _p(p),
        "confidence_interval": [_num(ci[0]), _num(ci[1])],
        "alpha": alpha,
        "significant": bool(p < alpha),
        "interpretation": (
            f"Sample mean of {column} ({mean:.4g}) vs hypothesised {popmean:.4g}: "
            f"difference is {_sig(p, alpha)}."
        ),
    }


def t_test_two_sample(df, column, group_column=None, column2=None,
                      equal_var=True, paired=False, alpha=0.05,
                      check_assumptions=False):
    if group_column:
        (n1, a), (n2, b) = _split_two(df, column, group_column)
        label1, label2 = f"{group_column}={n1}", f"{group_column}={n2}"
    elif column2:
        a, b = _numeric(df, column), _numeric(df, column2)
        label1, label2 = column, column2
    else:
        raise AnalyticsError("Provide either 'group_column' or 'column2'.")

    if paired:
        if len(a) != len(b):
            raise AnalyticsError("Paired t-test needs equal-length samples.")
        t, p = stats.ttest_rel(a, b)
        kind = "paired"
    else:
        t, p = stats.ttest_ind(a, b, equal_var=equal_var)
        kind = "Student" if equal_var else "Welch"

    d = _cohens_d(a, b, paired=paired)
    result = {
        "test": "two_sample_t_test",
        "variant": kind,
        "group1": {"label": label1, "n": len(a), "mean": _num(float(np.mean(a))),
                   "std": _num(float(np.std(a, ddof=1))), "ci": _ci_mean(a, alpha)},
        "group2": {"label": label2, "n": len(b), "mean": _num(float(np.mean(b))),
                   "std": _num(float(np.std(b, ddof=1))), "ci": _ci_mean(b, alpha)},
        "mean_difference": _num(float(np.mean(a) - np.mean(b))),
        "t_statistic": _num(float(t)),
        "p_value": _p(p),
        "cohens_d": _num(d),
        "effect_magnitude": _d_magnitude(d),
        "alpha": alpha,
        "significant": bool(p < alpha),
        "interpretation": (
            f"Means of {label1} ({np.mean(a):.4g}) and {label2} ({np.mean(b):.4g}) "
            f"differ — {_sig(p, alpha)} ({kind}'s t-test; "
            f"Cohen's d={d:.2f}, {_d_magnitude(d)} effect)."
        ),
    }

    if check_assumptions:
        assumptions = _assume({label1: a, label2: b}, alpha)
        result["assumptions"] = assumptions
        if assumptions["warnings"]:
            result["warning"] = " ".join(assumptions["warnings"])
        if assumptions["recommended"] == "nonparametric" and not paired:
            try:
                u, pu = stats.mannwhitneyu(a, b, alternative="two-sided")
                result["nonparametric"] = {
                    "test": "mann_whitney_u", "statistic": _num(float(u)),
                    "p_value": _p(pu), "significant": bool(pu < alpha),
                }
            except Exception:  # noqa: BLE001
                pass
    return result


# --------------------------------------------------------------------------
# ANOVA (one-way) + Tukey HSD
# --------------------------------------------------------------------------
def anova_oneway(df, value_column, group_column, alpha=0.05, posthoc=True,
                 check_assumptions=False):
    groups = _named_groups(df, value_column, group_column)
    arrays = list(groups.values())
    f, p = stats.f_oneway(*arrays)

    grand = np.concatenate(arrays)
    k, n = len(arrays), len(grand)
    ss_between = sum(len(g) * (np.mean(g) - np.mean(grand)) ** 2 for g in arrays)
    ss_total = float(np.sum((grand - np.mean(grand)) ** 2))
    eta_sq = (ss_between / ss_total) if ss_total else 0.0

    summary = [
        {"group": str(g), "n": len(v), "mean": _num(float(np.mean(v))),
         "std": _num(float(np.std(v, ddof=1)) if len(v) > 1 else 0.0),
         "ci": _ci_mean(v, alpha)}
        for g, v in groups.items()
    ]
    summary.sort(key=lambda r: (r["mean"] is None, -(r["mean"] or 0)))

    result = {
        "test": "one_way_anova",
        "value_column": value_column,
        "group_column": group_column,
        "groups": len(arrays),
        "f_statistic": _num(float(f)),
        "p_value": _p(p),
        "df_between": k - 1,
        "df_within": n - k,
        "eta_squared": _num(eta_sq),
        "group_summary": summary,
        "alpha": alpha,
        "significant": bool(p < alpha),
        "interpretation": (
            f"Mean {value_column} across {k} {group_column} groups: differences are "
            f"{_sig(p, alpha)} (η²={eta_sq:.3f}, "
            f"{'large' if eta_sq >= 0.14 else 'medium' if eta_sq >= 0.06 else 'small'} effect). "
            + (f"Highest: {summary[0]['group']} ({summary[0]['mean']}); "
               f"lowest: {summary[-1]['group']} ({summary[-1]['mean']})."
               if summary else "")
        ),
    }

    if posthoc and p < alpha and len(arrays) >= 3:
        try:
            from statsmodels.stats.multicomp import pairwise_tukeyhsd
            labels, data = [], []
            for g, v in groups.items():
                labels += [str(g)] * len(v)
                data += list(v)
            tuk = pairwise_tukeyhsd(np.array(data), np.array(labels), alpha=alpha)
            pairs = []
            for row in tuk.summary().data[1:]:
                pairs.append({
                    "group1": row[0], "group2": row[1],
                    "mean_diff": _num(float(row[2])),
                    "p_adj": _p(float(row[3])),
                    "reject_h0": bool(row[6]),
                })
            result["tukey_hsd"] = sorted(
                pairs, key=lambda r: (not r["reject_h0"], r["p_adj"] or 1)
            )[:25]
        except Exception as exc:  # noqa: BLE001
            result["tukey_hsd_error"] = str(exc)

    if check_assumptions:
        assumptions = _assume(groups, alpha)
        result["assumptions"] = assumptions
        if assumptions["warnings"]:
            result["warning"] = " ".join(assumptions["warnings"])
        if assumptions["recommended"] == "nonparametric":
            try:
                h, ph = stats.kruskal(*arrays)
                result["nonparametric"] = {
                    "test": "kruskal_wallis", "statistic": _num(float(h)),
                    "p_value": _p(ph), "significant": bool(ph < alpha),
                }
            except Exception:  # noqa: BLE001
                pass
    return result


# --------------------------------------------------------------------------
# F-test (equality of variances) + Levene
# --------------------------------------------------------------------------
def f_test_variances(df, column, group_column=None, column2=None, alpha=0.05):
    if group_column:
        (n1, a), (n2, b) = _split_two(df, column, group_column)
        label1, label2 = f"{group_column}={n1}", f"{group_column}={n2}"
    elif column2:
        a, b = _numeric(df, column), _numeric(df, column2)
        label1, label2 = column, column2
    else:
        raise AnalyticsError("Provide either 'group_column' or 'column2'.")

    v1, v2 = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
    f = v1 / v2 if v2 else np.inf
    df1, df2 = len(a) - 1, len(b) - 1
    # two-sided F p-value
    cdf = stats.f.cdf(f, df1, df2)
    p_f = 2 * min(cdf, 1 - cdf)
    w, p_lev = stats.levene(a, b)  # robust to non-normality

    return {
        "test": "f_test_equal_variance",
        "group1": {"label": label1, "n": len(a), "variance": _num(v1)},
        "group2": {"label": label2, "n": len(b), "variance": _num(v2)},
        "f_statistic": _num(float(f)),
        "df1": df1, "df2": df2,
        "p_value": _p(p_f),
        "levene": {"statistic": _num(float(w)), "p_value": _p(p_lev)},
        "alpha": alpha,
        "equal_variances": bool(p_lev > alpha),
        "interpretation": (
            f"Variances of {label1} ({v1:.4g}) and {label2} ({v2:.4g}): "
            f"{_sig(p_lev, alpha)} difference (Levene). "
            + ("Use Student's t-test." if p_lev > alpha else "Use Welch's t-test.")
        ),
    }


# --------------------------------------------------------------------------
# Chi-square (independence)
# --------------------------------------------------------------------------
def chi_square_test(df, column1, column2, alpha=0.05):
    for c in (column1, column2):
        if c not in df.columns:
            raise AnalyticsError(f"Column '{c}' not found.")
    table = pd.crosstab(df[column1].astype(str), df[column2].astype(str))
    if table.size == 0 or table.shape[0] < 2 or table.shape[1] < 2:
        raise AnalyticsError("Need at least a 2×2 contingency table.")
    chi2, p, dof, expected = stats.chi2_contingency(table)
    n = int(table.values.sum())
    min_dim = min(table.shape) - 1
    cramers_v = float(np.sqrt(chi2 / (n * min_dim))) if n and min_dim else 0.0
    low_expected = int((expected < 5).sum())
    return {
        "test": "chi_square_independence",
        "column1": column1, "column2": column2,
        "chi2_statistic": _num(float(chi2)),
        "p_value": _p(p),
        "dof": int(dof),
        "n": n,
        "cramers_v": _num(cramers_v),
        "contingency_shape": list(table.shape),
        "cells_expected_lt5": low_expected,
        "alpha": alpha,
        "significant": bool(p < alpha),
        "interpretation": (
            f"Association between {column1} and {column2}: {_sig(p, alpha)} "
            f"(Cramér's V={cramers_v:.3f}, "
            f"{'strong' if cramers_v >= 0.5 else 'moderate' if cramers_v >= 0.3 else 'weak'})."
            + (" ⚠ Some expected counts <5 — interpret with caution."
               if low_expected else "")
        ),
    }


# --------------------------------------------------------------------------
# Correlation (Pearson / Spearman / Kendall) with p-value
# --------------------------------------------------------------------------
def correlation_test(df, column1, column2, method="pearson"):
    x = pd.to_numeric(df[column1], errors="coerce") if column1 in df.columns else None
    y = pd.to_numeric(df[column2], errors="coerce") if column2 in df.columns else None
    if x is None or y is None:
        raise AnalyticsError("Both columns must exist and be numeric.")
    pair = pd.concat([x, y], axis=1).dropna()
    if len(pair) < 3:
        raise AnalyticsError("Need at least 3 paired numeric observations.")
    a, b = pair.iloc[:, 0].to_numpy(), pair.iloc[:, 1].to_numpy()
    method = (method or "pearson").lower()
    fn = {"pearson": stats.pearsonr, "spearman": stats.spearmanr,
          "kendall": stats.kendalltau}.get(method)
    if fn is None:
        raise AnalyticsError("method must be pearson, spearman or kendall.")
    r, p = fn(a, b)
    return {
        "test": "correlation",
        "method": method,
        "column1": column1, "column2": column2,
        "n": len(pair),
        "coefficient": _num(float(r)),
        "r_squared": _num(float(r) ** 2),
        "p_value": _p(p),
        "significant": bool(p < 0.05),
        "interpretation": (
            f"{method.title()} correlation between {column1} and {column2} is "
            f"{_corr_strength(float(r))} (r={float(r):.3f}, {_sig(p, 0.05)})."
        ),
    }


# --------------------------------------------------------------------------
# Regression — linear (OLS), polynomial, non-linear
# --------------------------------------------------------------------------
_MAX_FIT_POINTS = 500


def _fit_points(x, y, predicted):
    idx = np.argsort(x)
    xs, ys, ps = x[idx], y[idx], predicted[idx]
    if len(xs) > _MAX_FIT_POINTS:
        sel = np.linspace(0, len(xs) - 1, _MAX_FIT_POINTS).astype(int)
        xs, ys, ps = xs[sel], ys[sel], ps[sel]
    return [[_num(float(a)), _num(float(b)), _num(float(c))]
            for a, b, c in zip(xs, ys, ps)]


def regression(df, y_column, x_columns=None, x_column=None, kind="linear",
               degree=2, model=None):
    kind = (kind or "linear").lower()
    y = pd.to_numeric(df[y_column], errors="coerce") if y_column in df.columns else None
    if y is None:
        raise AnalyticsError(f"y_column '{y_column}' must be numeric.")

    # ---- Linear (one or many predictors) via statsmodels OLS ----
    if kind == "linear":
        xs = x_columns or ([x_column] if x_column else None)
        if not xs:
            raise AnalyticsError("Provide x_columns (or x_column) for linear regression.")
        import statsmodels.api as sm
        frame = df[[*xs, y_column]].apply(pd.to_numeric, errors="coerce").dropna()
        if len(frame) <= len(xs) + 1:
            raise AnalyticsError("Not enough complete rows for the regression.")
        X = sm.add_constant(frame[xs].to_numpy(float))
        Y = frame[y_column].to_numpy(float)
        res = sm.OLS(Y, X).fit()
        names = ["intercept", *xs]
        coeffs = [{"term": names[i], "coef": _num(float(res.params[i])),
                   "p_value": _p(float(res.pvalues[i]))} for i in range(len(names))]
        out = {
            "test": "linear_regression",
            "y_column": y_column, "x_columns": list(xs), "n": int(res.nobs),
            "coefficients": coeffs,
            "r_squared": _num(float(res.rsquared)),
            "adj_r_squared": _num(float(res.rsquared_adj)),
            "f_pvalue": _p(float(res.f_pvalue)),
            "equation": _linear_equation(names, res.params),
            "interpretation": (
                f"Linear model for {y_column} ~ {' + '.join(xs)}: "
                f"R²={res.rsquared:.3f} ({res.rsquared*100:.1f}% of variance explained), "
                f"overall fit {_sig(res.f_pvalue, 0.05)}."
            ),
        }
        if len(xs) == 1:
            x1 = frame[xs[0]].to_numpy(float)
            out["fit_points"] = _fit_points(x1, Y, res.fittedvalues)
            out["columns"] = [xs[0], "actual", "predicted"]
        return out

    # ---- Polynomial (single predictor) ----
    xc = x_column or (x_columns[0] if x_columns else None)
    if not xc:
        raise AnalyticsError("Polynomial/non-linear regression needs x_column.")
    pair = pd.concat([pd.to_numeric(df[xc], errors="coerce"), y], axis=1).dropna()
    if len(pair) < 3:
        raise AnalyticsError("Need at least 3 paired points.")
    x = pair.iloc[:, 0].to_numpy(float)
    yv = pair.iloc[:, 1].to_numpy(float)

    if kind == "polynomial":
        degree = max(2, min(int(degree or 2), 6))
        coef = np.polyfit(x, yv, degree)
        pred = np.polyval(coef, x)
        r2 = _r2(yv, pred)
        return {
            "test": "polynomial_regression", "y_column": y_column, "x_column": xc,
            "degree": degree, "n": len(x),
            "coefficients": [_num(float(c)) for c in coef],
            "r_squared": _num(r2),
            "equation": _poly_equation(coef),
            "fit_points": _fit_points(x, yv, pred),
            "columns": [xc, "actual", "predicted"],
            "interpretation": (
                f"Degree-{degree} polynomial for {y_column} vs {xc}: R²={r2:.3f}."
            ),
        }

    if kind in ("nonlinear", "non-linear"):
        return _nonlinear(x, yv, xc, y_column, model)

    raise AnalyticsError("kind must be linear, polynomial or nonlinear.")


def _r2(y, pred):
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return (1 - ss_res / ss_tot) if ss_tot else 1.0


def _linear_equation(names, params):
    terms = [f"{params[0]:.4g}"]
    for i in range(1, len(names)):
        terms.append(f"{params[i]:+.4g}·{names[i]}")
    return "y = " + " ".join(terms)


def _poly_equation(coef):
    d = len(coef) - 1
    parts = []
    for i, c in enumerate(coef):
        power = d - i
        if power == 0:
            parts.append(f"{c:+.4g}")
        elif power == 1:
            parts.append(f"{c:+.4g}·x")
        else:
            parts.append(f"{c:+.4g}·x^{power}")
    return "y = " + " ".join(parts).lstrip("+")


def _nonlinear(x, y, xc, yc, model):
    forms = {
        "exponential": (lambda x, a, b: a * np.exp(b * x), "y = a·e^(b·x)"),
        "logarithmic": (lambda x, a, b: a + b * np.log(x), "y = a + b·ln(x)"),
        "power": (lambda x, a, b: a * np.power(x, b), "y = a·x^b"),
    }
    candidates = [model] if model in forms else list(forms)
    best = None
    for name in candidates:
        fn, eq = forms[name]
        try:
            if name in ("logarithmic", "power") and np.any(x <= 0):
                continue
            popt, _ = optimize.curve_fit(fn, x, y, maxfev=10000)
            r2 = _r2(y, fn(x, *popt))
            if best is None or r2 > best["r_squared_raw"]:
                best = {"model": name, "equation": eq,
                        "params": [float(p) for p in popt],
                        "r_squared_raw": r2, "fn": fn}
        except Exception:  # noqa: BLE001 - some forms won't converge
            continue
    if best is None:
        raise AnalyticsError("No non-linear model converged on this data.")
    pred = best["fn"](x, *best["params"])
    return {
        "test": "nonlinear_regression", "y_column": yc, "x_column": xc,
        "model": best["model"], "equation": best["equation"],
        "parameters": [_num(p) for p in best["params"]],
        "r_squared": _num(best["r_squared_raw"]), "n": len(x),
        "fit_points": _fit_points(x, y, pred),
        "columns": [xc, "actual", "predicted"],
        "interpretation": (
            f"Best non-linear fit for {yc} vs {xc} is {best['model']} "
            f"({best['equation']}): R²={best['r_squared_raw']:.3f}."
        ),
    }


# --------------------------------------------------------------------------
# Outlier & box-plot analysis
# --------------------------------------------------------------------------
def outlier_analysis(df, column):
    y = _numeric(df, column)
    q1, med, q3 = (float(v) for v in np.percentile(y, [25, 50, 75]))
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    far_lo, far_hi = q1 - 3 * iqr, q3 + 3 * iqr
    mean, std = float(np.mean(y)), float(np.std(y, ddof=1))
    med_abs = float(np.median(np.abs(y - med))) or 1e-9

    rows = []
    for i, v in enumerate(y):
        z = (v - mean) / std if std else 0.0
        mz = 0.6745 * (v - med) / med_abs
        if v < lo or v > hi:
            rows.append({"index": i, "value": _num(float(v)),
                         "z_score": _num(z), "modified_z": _num(mz),
                         "extreme": bool(v < far_lo or v > far_hi)})
    return {
        "test": "outlier_analysis",
        "column": column, "n": len(y),
        "box_plot": {"min": _num(float(np.min(y))), "q1": _num(q1),
                     "median": _num(med), "q3": _num(q3),
                     "max": _num(float(np.max(y))),
                     "whisker_low": _num(max(lo, float(np.min(y)))),
                     "whisker_high": _num(min(hi, float(np.max(y))))},
        "fences": {"inner_low": _num(lo), "inner_high": _num(hi),
                   "outer_low": _num(far_lo), "outer_high": _num(far_hi)},
        "outlier_count": len(rows),
        "extreme_count": sum(r["extreme"] for r in rows),
        "outliers": rows[:100],
        "interpretation": (
            f"{column}: {len(rows)} outlier(s) beyond 1.5×IQR "
            f"({sum(r['extreme'] for r in rows)} extreme beyond 3×IQR) "
            f"out of {len(y)} values."
        ),
    }


# --------------------------------------------------------------------------
# Control charts (I-MR for individuals)
# --------------------------------------------------------------------------
_D2 = 1.128  # d2 for moving range of size 2


def control_chart(df, value_column, index_column=None):
    y = _numeric(df, value_column)
    if len(y) < 5:
        raise AnalyticsError("Need at least 5 points for a control chart.")
    cl = float(np.mean(y))
    mr = np.abs(np.diff(y))
    mr_bar = float(np.mean(mr)) if len(mr) else 0.0
    sigma = mr_bar / _D2 if mr_bar else float(np.std(y, ddof=1))
    ucl, lcl = cl + 3 * sigma, cl - 3 * sigma

    if index_column and index_column in df.columns:
        labels = df[index_column].astype(str).tolist()[:len(y)]
    else:
        labels = list(range(len(y)))

    violations = []
    for i, v in enumerate(y):
        if v > ucl or v < lcl:
            violations.append({"index": i, "label": str(labels[i]),
                               "value": _num(float(v)), "rule": "beyond_3sigma"})
    # Western Electric rule 2: 8 consecutive points one side of the centre line
    run, side = 0, 0
    for i, v in enumerate(y):
        cur = 1 if v > cl else -1 if v < cl else 0
        run = run + 1 if cur == side and cur != 0 else 1
        side = cur
        if run >= 8:
            violations.append({"index": i, "label": str(labels[i]),
                               "value": _num(float(v)), "rule": "run_8_one_side"})

    n = len(y)
    points = ([{"label": str(labels[i]), "value": _num(float(y[i]))}
               for i in range(n)] if n <= 500 else None)
    return {
        "test": "control_chart_individuals",
        "value_column": value_column,
        "center_line": _num(cl), "ucl": _num(ucl), "lcl": _num(lcl),
        "sigma": _num(sigma), "mr_bar": _num(mr_bar), "n": n,
        "in_control": len(violations) == 0,
        "violation_count": len(violations),
        "violations": violations[:50],
        "points": points,
        "interpretation": (
            f"{value_column} I-chart: CL={cl:.4g}, UCL={ucl:.4g}, LCL={lcl:.4g}. "
            + ("Process is IN control." if not violations else
               f"{len(violations)} out-of-control signal(s) — process unstable.")
        ),
    }


# --------------------------------------------------------------------------
# Process capability (Cp / Cpk / Pp / Ppk)
# --------------------------------------------------------------------------
def process_capability(df, column, lsl=None, usl=None, target=None):
    if lsl is None and usl is None:
        raise AnalyticsError("Provide at least one of 'lsl' / 'usl'.")
    y = _numeric(df, column)
    mean, std_overall = float(np.mean(y)), float(np.std(y, ddof=1))
    mr_bar = float(np.mean(np.abs(np.diff(y)))) if len(y) > 1 else 0.0
    std_within = mr_bar / _D2 if mr_bar else std_overall
    lsl = None if lsl is None else float(lsl)
    usl = None if usl is None else float(usl)

    def _cap(s):
        cp = ((usl - lsl) / (6 * s)) if (lsl is not None and usl is not None and s) else None
        cpu = ((usl - mean) / (3 * s)) if (usl is not None and s) else None
        cpl = ((mean - lsl) / (3 * s)) if (lsl is not None and s) else None
        cpk = min([v for v in (cpu, cpl) if v is not None], default=None)
        return cp, cpk

    cp, cpk = _cap(std_within)
    pp, ppk = _cap(std_overall)

    below = float(np.mean(y < lsl)) if lsl is not None else 0.0
    above = float(np.mean(y > usl)) if usl is not None else 0.0
    out_pct = (below + above) * 100
    capable = cpk is not None and cpk >= 1.33
    return {
        "test": "process_capability",
        "column": column, "n": len(y),
        "mean": _num(mean), "std_within": _num(std_within),
        "std_overall": _num(std_overall),
        "lsl": _num(lsl), "usl": _num(usl), "target": _num(target),
        "cp": _num(cp), "cpk": _num(cpk), "pp": _num(pp), "ppk": _num(ppk),
        "pct_out_of_spec": _num(out_pct),
        "dpmo": _num(out_pct * 10000),
        "capable": bool(capable),
        "interpretation": (
            f"{column}: Cpk={_num(cpk)}, Ppk={_num(ppk)}, {out_pct:.2f}% out of spec. "
            + ("Process is CAPABLE (Cpk≥1.33)." if capable else
               "Process is NOT capable (Cpk<1.33) — reduce variation or re-centre.")
        ),
    }


# --------------------------------------------------------------------------
# Trend analysis (linear slope + Mann-Kendall)
# --------------------------------------------------------------------------
def trend_analysis(df, value_column, index_column=None):
    y = _numeric(df, value_column)
    n = len(y)
    if n < 4:
        raise AnalyticsError("Need at least 4 points for trend analysis.")
    x = np.arange(n)
    slope, intercept, r, p, se = stats.linregress(x, y)
    direction = ("increasing" if slope > 0 and p < 0.05 else
                 "decreasing" if slope < 0 and p < 0.05 else "no significant trend")

    mk = _mann_kendall(y) if n <= 1200 else None
    pct_change = float((y[-1] - y[0]) / abs(y[0]) * 100) if y[0] else None
    return {
        "test": "trend_analysis",
        "value_column": value_column, "n": n,
        "slope": _num(float(slope)), "intercept": _num(float(intercept)),
        "r_squared": _num(float(r) ** 2), "p_value": _p(p),
        "direction": direction,
        "pct_change_overall": _num(pct_change),
        "mann_kendall": mk,
        "interpretation": (
            f"{value_column}: linear slope {slope:+.4g}/step (R²={r**2:.3f}, {_sig(p, 0.05)}); "
            f"trend is {direction}."
            + (f" Mann-Kendall τ={mk['tau']} (p={mk['p_value']})." if mk else "")
        ),
    }


def _mann_kendall(y):
    n = len(y)
    s = 0
    for i in range(n - 1):
        s += np.sum(np.sign(y[i + 1:] - y[i]))
    var_s = (n * (n - 1) * (2 * n + 5)) / 18.0
    if s > 0:
        z = (s - 1) / np.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / np.sqrt(var_s)
    else:
        z = 0.0
    p = 2 * (1 - stats.norm.cdf(abs(z)))
    tau = s / (0.5 * n * (n - 1))
    return {"tau": _num(float(tau)), "p_value": _p(float(p)),
            "z": _num(float(z)),
            "trend": ("increasing" if z > 0 and p < 0.05 else
                      "decreasing" if z < 0 and p < 0.05 else "none")}


# --------------------------------------------------------------------------
# Batch descriptive statistics (all numeric columns at once)
# --------------------------------------------------------------------------
def batch_descriptive(df, columns=None):
    """Descriptive statistics for every numeric column (or a given subset).

    Returns a table (columns + rows) so the frontend can render it directly —
    the one-click "describe everything" for the statistics dashboard.
    """
    total = len(df)
    if columns:
        candidates = [c for c in columns if c in df.columns]
    else:
        candidates = []
        for c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce")
            if s.notna().sum() >= 2 and s.notna().mean() >= 0.8:
                candidates.append(c)
    if not candidates:
        raise AnalyticsError("No numeric columns to summarise.")

    rows = []
    for c in candidates:
        try:
            y = _numeric(df, c)
        except AnalyticsError:
            continue
        n = len(y)
        std = float(np.std(y, ddof=1)) if n > 1 else 0.0
        q1, med, q3 = (float(v) for v in np.percentile(y, [25, 50, 75]))
        rows.append({
            "column": c, "n": n,
            "mean": _num(float(np.mean(y))), "median": _num(med), "std": _num(std),
            "min": _num(float(np.min(y))), "q1": _num(q1), "q3": _num(q3),
            "max": _num(float(np.max(y))),
            "skewness": _num(float(stats.skew(y))) if n > 2 else None,
            "kurtosis": _num(float(stats.kurtosis(y))) if n > 3 else None,
            "missing_pct": _num((1 - n / total) * 100) if total else None,
        })
    if not rows:
        raise AnalyticsError("No numeric columns to summarise.")
    return {
        "test": "batch_descriptive",
        "n_columns": len(rows),
        "columns": ["column", "n", "mean", "median", "std", "min", "q1", "q3",
                    "max", "skewness", "kurtosis", "missing_pct"],
        "rows": rows,
        "interpretation": (
            f"Descriptive statistics across {len(rows)} numeric column(s) "
            f"({total} rows)."
        ),
    }


# --------------------------------------------------------------------------
# Assumption checks (normality + variance homogeneity)
# --------------------------------------------------------------------------
def check_assumptions(df, value_column=None, group_column=None, column=None,
                      column2=None, columns=None, alpha=0.05):
    """Normality (Shapiro) + variance homogeneity (Levene) for a comparison,
    with a parametric / non-parametric recommendation."""
    if value_column and group_column:
        groups = _named_groups(df, value_column, group_column, min_n=2)
    elif column and column2:
        groups = {column: _numeric(df, column), column2: _numeric(df, column2)}
    elif columns:
        groups = {c: _numeric(df, c) for c in columns if c in df.columns}
    elif column:
        groups = {column: _numeric(df, column)}
    else:
        raise AnalyticsError(
            "Provide value_column+group_column, column(+column2), or columns."
        )
    if not groups:
        raise AnalyticsError("No usable columns/groups for assumption checks.")

    res = _assume(groups, alpha)
    res["test"] = "assumption_checks"
    res["groups_checked"] = [str(g) for g in groups]
    res["interpretation"] = (
        f"{'All groups appear ~normal' if res['all_normal'] else 'Non-normality detected'}; "
        f"variances {'homogeneous' if res['equal_variance'] else 'unequal'}. "
        f"Recommended approach: {res['recommended']}."
    )
    return res
