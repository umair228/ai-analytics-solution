"""Registry + dispatcher for the statistical tests in :mod:`analytics.stats_tests`.

ONE source of truth, consumed by both the REST API (``analytics.views``) and the
agent's ``run_statistical_test`` tool. Each entry declares the test's parameters
(with a ``kind`` the frontend uses to render the right column/value picker), so
the statistical dashboard form is fully data-driven.
"""
from __future__ import annotations

from .engine import AnalyticsError
from . import spc as SPC
from . import stats_tests as S


def _p(name, kind, required=False, default=None, help="", options=None):
    p = {"name": name, "kind": kind, "required": required, "help": help}
    if default is not None:
        p["default"] = default
    if options:
        p["options"] = options
    return p


# kind ∈ numeric_column | category_column | column | columns | number | enum | bool | text
TESTS = {
    "descriptive": {
        "fn": S.descriptive_stats, "label": "Descriptive Statistics",
        "category": "Descriptive",
        "description": "Mean, median, std, quartiles, skewness, kurtosis for a column.",
        "params": [_p("column", "numeric_column", True, help="Numeric column.")],
    },
    "descriptive_all": {
        "fn": S.batch_descriptive, "label": "Descriptive (All Columns)",
        "category": "Descriptive",
        "description": "Mean, median, std, quartiles, skew/kurtosis for every numeric column at once.",
        "params": [_p("columns", "columns", help="Optional subset (default: all numeric).")],
    },
    "normality": {
        "fn": S.normality_test, "label": "Normality Test",
        "category": "Descriptive",
        "description": "Shapiro-Wilk, D'Agostino K² and Anderson-Darling normality tests.",
        "params": [_p("column", "numeric_column", True),
                   _p("alpha", "number", default=0.05)],
    },
    "trend": {
        "fn": S.trend_analysis, "label": "Trend Analysis",
        "category": "Descriptive",
        "description": "Linear slope + Mann-Kendall monotonic-trend test over a series.",
        "params": [_p("value_column", "numeric_column", True),
                   _p("index_column", "column", help="Optional ordering/label column.")],
    },
    "t_test_one_sample": {
        "fn": S.t_test_one_sample, "label": "One-Sample t-Test",
        "category": "Comparison",
        "description": "Compare a column's mean against a hypothesised value.",
        "params": [_p("column", "numeric_column", True),
                   _p("popmean", "number", True, help="Hypothesised mean."),
                   _p("alpha", "number", default=0.05)],
    },
    "t_test_two_sample": {
        "fn": S.t_test_two_sample, "label": "Two-Sample t-Test",
        "category": "Comparison",
        "description": "Compare means of two groups (split by a category) or two columns.",
        "params": [_p("column", "numeric_column", True, help="Value column."),
                   _p("group_column", "category_column", help="Split into 2 groups by this."),
                   _p("column2", "numeric_column", help="…or compare against this column."),
                   _p("equal_var", "bool", default=True, help="Equal variances (Student) vs Welch."),
                   _p("paired", "bool", default=False),
                   _p("alpha", "number", default=0.05),
                   _p("check_assumptions", "bool", default=False,
                      help="Run normality & equal-variance checks (advisory).")],
    },
    "anova_oneway": {
        "fn": S.anova_oneway, "label": "One-Way ANOVA",
        "category": "Comparison",
        "description": "Compare a measure's mean across 2+ groups, with Tukey HSD post-hoc.",
        "params": [_p("value_column", "numeric_column", True),
                   _p("group_column", "category_column", True),
                   _p("alpha", "number", default=0.05),
                   _p("posthoc", "bool", default=True),
                   _p("check_assumptions", "bool", default=False,
                      help="Run normality & equal-variance checks (advisory).")],
    },
    "assumptions": {
        "fn": S.check_assumptions, "label": "Assumption Checks",
        "category": "Comparison",
        "description": "Normality (Shapiro) + equal-variance (Levene) checks with a parametric/non-parametric recommendation.",
        "params": [_p("value_column", "numeric_column", help="Value column (with group_column)."),
                   _p("group_column", "category_column", help="Split into groups by this."),
                   _p("column", "numeric_column", help="…or a single column."),
                   _p("column2", "numeric_column", help="…compared against this column.")],
    },
    "f_test_variances": {
        "fn": S.f_test_variances, "label": "F-Test (Equal Variance)",
        "category": "Comparison",
        "description": "Test equality of variances (F-test + Levene) between two groups/columns.",
        "params": [_p("column", "numeric_column", True),
                   _p("group_column", "category_column"),
                   _p("column2", "numeric_column"),
                   _p("alpha", "number", default=0.05)],
    },
    "chi_square": {
        "fn": S.chi_square_test, "label": "Chi-Square Test",
        "category": "Relationship",
        "description": "Test independence between two categorical columns (+ Cramér's V).",
        "params": [_p("column1", "category_column", True),
                   _p("column2", "category_column", True),
                   _p("alpha", "number", default=0.05)],
    },
    "correlation": {
        "fn": S.correlation_test, "label": "Correlation",
        "category": "Relationship",
        "description": "Pearson / Spearman / Kendall correlation with p-value.",
        "params": [_p("column1", "numeric_column", True),
                   _p("column2", "numeric_column", True),
                   _p("method", "enum", default="pearson",
                      options=["pearson", "spearman", "kendall"])],
    },
    "regression": {
        "fn": S.regression, "label": "Regression",
        "category": "Relationship",
        "description": "Linear (1+ predictors), polynomial, or non-linear regression.",
        "params": [_p("y_column", "numeric_column", True, help="Dependent variable."),
                   _p("x_column", "numeric_column", help="Single predictor (poly/non-linear)."),
                   _p("x_columns", "columns", help="One or more predictors (linear)."),
                   _p("kind", "enum", default="linear",
                      options=["linear", "polynomial", "nonlinear"]),
                   _p("degree", "number", default=2, help="Polynomial degree."),
                   _p("model", "enum", help="Non-linear form.",
                      options=["exponential", "logarithmic", "power"])],
    },
    "outliers": {
        "fn": S.outlier_analysis, "label": "Outlier & Box-Plot",
        "category": "Quality",
        "description": "Box-plot five-number summary, IQR fences and flagged outliers.",
        "params": [_p("column", "numeric_column", True)],
    },
    # ── Quality / SPC ──────────────────────────────────────────────────────
    # `rules` is the lever that matters: a chart is only as good as the rules run
    # against it. Westgard is the clinical/lab QC standard; Nelson is the general
    # SPC set. See analytics.spc.
    "levey_jennings": {
        "fn": SPC.levey_jennings, "label": "Levey-Jennings (QC) + Westgard",
        "category": "Quality",
        "description": ("QC control chart for a control material with Westgard multirules "
                        "(1₂ₛ, 1₃ₛ, 2₂ₛ, R₄ₛ, 4₁ₛ, 10ₓ). Supply the material's assigned "
                        "mean/SD — limits fitted to the data cannot detect a shift the "
                        "data already contains."),
        "params": [_p("value_column", "numeric_column", True, help="The QC result."),
                   _p("mean", "number", help="Assigned mean of the control material."),
                   _p("sd", "number", help="Assigned SD of the control material."),
                   _p("index_column", "column", help="Run / date label."),
                   _p("material", "text", help="Control material or lot name."),
                   _p("rules", "enum", default="westgard",
                      options=["westgard", "westgard_extended", "nelson",
                               "western_electric", "basic"])],
    },
    "control_chart": {
        "fn": SPC.i_mr_chart, "label": "Control Chart (I-MR)",
        "category": "Quality",
        "description": "Individuals + moving-range chart with 3σ limits and the full rule set.",
        "params": [_p("value_column", "numeric_column", True),
                   _p("index_column", "column", help="Optional ordering/label column."),
                   _p("rules", "enum", default="nelson",
                      options=["nelson", "western_electric", "westgard", "basic"])],
    },
    "control_chart_xbar_r": {
        "fn": SPC.xbar_r_chart, "label": "Control Chart (X̄-R)",
        "category": "Quality",
        "description": "Subgroup means with sigma from the average range (subgroups of 2–10).",
        "params": [_p("value_column", "numeric_column", True),
                   _p("subgroup_column", "column", help="Group by this column (e.g. batch)."),
                   _p("subgroup_size", "number", default=5,
                      help="Used when no subgroup column is given."),
                   _p("rules", "enum", default="nelson",
                      options=["nelson", "western_electric", "basic"])],
    },
    "control_chart_xbar_s": {
        "fn": SPC.xbar_s_chart, "label": "Control Chart (X̄-S)",
        "category": "Quality",
        "description": "Subgroup means with sigma from the average subgroup SD (larger subgroups).",
        "params": [_p("value_column", "numeric_column", True),
                   _p("subgroup_column", "column", help="Group by this column."),
                   _p("subgroup_size", "number", default=10),
                   _p("rules", "enum", default="nelson",
                      options=["nelson", "western_electric", "basic"])],
    },
    "control_chart_ewma": {
        "fn": SPC.ewma_chart, "label": "EWMA Chart (drift)",
        "category": "Quality",
        "description": ("Exponentially-weighted moving average — catches the small sustained "
                        "shift (~1σ) a Shewhart chart is nearly blind to, which is how a "
                        "drifting instrument or a degrading reagent lot presents."),
        "params": [_p("value_column", "numeric_column", True),
                   _p("lam", "number", default=0.1, help="Smoothing (0–1); smaller = longer memory."),
                   _p("L", "number", default=2.7, help="Limit width in sigma."),
                   _p("mean", "number",
                      help="IN-CONTROL target mean. Leave blank and it is estimated from "
                           "these points — which already contain the shift, so the chart "
                           "under-reports it."),
                   _p("sd", "number", help="Process SD (default: estimated from MR̄)."),
                   _p("index_column", "column")],
    },
    "control_chart_cusum": {
        "fn": SPC.cusum_chart, "label": "CUSUM Chart (drift)",
        "category": "Quality",
        "description": "Cumulative sum of deviation from target — fastest detector of a sustained shift.",
        "params": [_p("value_column", "numeric_column", True),
                   _p("target", "number", help="Target value (default: mean)."),
                   _p("k", "number", default=0.5, help="Slack, in sigma."),
                   _p("h", "number", default=4.0, help="Decision interval, in sigma."),
                   _p("sd", "number", help="Process SD (default: estimated from MR̄)."),
                   _p("index_column", "column")],
    },
    "control_chart_attribute": {
        "fn": SPC.attribute_chart, "label": "Attribute Chart (p / np / c / u)",
        "category": "Quality",
        "description": ("Charts for counted data. A p-chart of 'proportion of results "
                        "off-spec per day' needs no data the LIMS doesn't already have."),
        "params": [_p("chart", "enum", default="p", options=["p", "np", "c", "u"]),
                   _p("count_column", "numeric_column", True, help="Nonconforming / defect count."),
                   _p("size_column", "numeric_column", help="Subgroup size (p, np, u)."),
                   _p("index_column", "column")],
    },
    "process_capability": {
        "fn": S.process_capability, "label": "Process Capability",
        "category": "Quality",
        "description": "Cp / Cpk / Pp / Ppk against spec limits, with % out-of-spec & DPMO.",
        "params": [_p("column", "numeric_column", True),
                   _p("lsl", "number", help="Lower spec limit."),
                   _p("usl", "number", help="Upper spec limit."),
                   _p("target", "number", help="Optional target.")],
    },
}


def catalog() -> list[dict]:
    """The list of tests + parameter schemas (drives the dashboard form)."""
    return [
        {"name": name, "label": t["label"], "category": t["category"],
         "description": t["description"], "params": t["params"]}
        for name, t in TESTS.items()
    ]


def test_names() -> list[str]:
    return list(TESTS)


def _clean_params(params: dict) -> dict:
    """Drop empty optionals so the function defaults apply."""
    out = {}
    for k, v in (params or {}).items():
        if v is None or v == "" or (isinstance(v, list) and not v):
            continue
        out[k] = v
    return out


def run_test(name: str, df, params: dict) -> dict:
    spec = TESTS.get(name)
    if spec is None:
        raise AnalyticsError(
            f"Unknown statistical test '{name}'. Available: {', '.join(TESTS)}."
        )
    allowed = {p["name"] for p in spec["params"]}
    clean = {k: v for k, v in _clean_params(params).items() if k in allowed}
    return spec["fn"](df, **clean)
