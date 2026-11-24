"""Alert rules that fire on an ANALYSIS, not just a threshold on a column.

Until now an alert could only say "average(sulphur) > 12". That is a useful thing
to say and a poor thing to be limited to: it cannot express any of the questions a
laboratory actually monitors —

    "tell me when a Westgard rule fires on control material QC-101"
    "tell me when Cpk drops below 1.33"
    "tell me when the off-spec rate is forecast to breach 2% within a fortnight"

— because each of those is the *outcome of an analysis*, not a number in a column.
This module makes the analysis itself the alert condition.

Every rule routes through the SAME dispatchers the UI and the agent use
(``analytics.stats_catalog``, ``analytics.anomaly``), so an alert cannot disagree
with what a user sees on screen. Each returns a scalar, which the alert's existing
``condition``/``threshold`` then compares — so the "reject when > 0" machinery is
reused rather than reinvented.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Outcome:
    """What an analytic rule found. ``value`` is what the threshold is applied to."""

    value: float
    subject: str                       # "Cpk", "control-rule rejections", …
    summary: str                       # what HAPPENED, in a sentence a human reads
    detail: dict = field(default_factory=dict)


def _numeric(df, column):
    if not column or column not in df.columns:
        return None
    s = pd.to_numeric(df[column], errors="coerce").dropna()
    return None if s.empty else s


# --------------------------------------------------------------------------
# SPC — the flagship. "Alert me when a Westgard rule fires."
# --------------------------------------------------------------------------
def spc_rule(alert, df) -> Outcome | None:
    from analytics import stats_catalog

    cfg = dict(alert.config or {})
    test = cfg.pop("test", "levey_jennings")
    params = {"value_column": alert.column, **cfg}

    out = stats_catalog.run_test(test, df, params)
    rejects = int(out.get("reject_count", 0))
    violations = out.get("violations") or []
    fired = out.get("violations_by_rule") or {}
    errs = out.get("error_types") or []

    # Count REJECTION rules separately from warning rules. Lumping them together
    # would have the message say "9 rejections: 1₂ₛ ×6, 2₂ₛ ×4, 4₁ₛ ×5" — fifteen
    # items under a count of nine — which is precisely the warning/rejection
    # confusion the Westgard scheme exists to prevent.
    rejected_by_rule: dict[str, int] = {}
    warned_by_rule: dict[str, int] = {}
    for v in violations:
        bucket = rejected_by_rule if v.get("severity") == "reject" else warned_by_rule
        label = v.get("rule_label") or v.get("rule")
        bucket[label] = bucket.get(label, 0) + 1

    if rejects:
        rules = ", ".join(f"{k} ×{v}" for k, v in rejected_by_rule.items())
        # Naming the error type is the whole reason multirules exist: it is the
        # difference between "something is wrong" and "go look at the calibration".
        cause = ""
        if "systematic" in errs and "random" in errs:
            cause = " Both systematic (bias) and random (imprecision) error are indicated."
        elif "systematic" in errs:
            cause = (" Indicates SYSTEMATIC error — a shift or drift "
                     "(calibration, reagent lot, an instrument setting).")
        elif "random" in errs:
            cause = " Indicates RANDOM error — imprecision."
        warn = ""
        if warned_by_rule:
            warn = (" Also flagged (warnings, not rejections): "
                    + ", ".join(f"{k} ×{v}" for k, v in warned_by_rule.items()) + ".")
        last_reject = next((v.get("label") for v in reversed(violations)
                            if v.get("severity") == "reject"), "")
        summary = (f"{rejects} control-rule rejection(s) on {alert.column}"
                   f"{f' (latest at {last_reject})' if last_reject else ''}: "
                   f"{rules}.{cause}{warn}")
    else:
        summary = f"{alert.column} is in control — no rule violated."

    return Outcome(
        value=float(rejects),
        subject="control-rule rejections",
        summary=summary,
        detail={
            "test": out.get("test"), "rules_applied": out.get("rules_applied"),
            "violations_by_rule": fired,
            "rejections_by_rule": rejected_by_rule,
            "warnings_by_rule": warned_by_rule,
            "error_types": errs,
            "center_line": out.get("center_line"), "limits": out.get("limits"),
            "established_limits": out.get("established_limits"),
            "violations": (out.get("violations") or [])[:20],
            # If the limits were fitted to the data, the chart under-reports a shift.
            # An alert built on that is worth less than it looks — say so on the event.
            "warning": out.get("warning", ""),
        },
    )


# --------------------------------------------------------------------------
# Process capability — "alert me when Cpk drops below 1.33"
# --------------------------------------------------------------------------
def capability_rule(alert, df) -> Outcome | None:
    from analytics import stats_catalog

    cfg = dict(alert.config or {})
    out = stats_catalog.run_test("process_capability", df,
                                 {"column": alert.column, **cfg})
    cpk = out.get("cpk")
    if cpk is None:
        # No spec limit means no capability. Firing on a missing number would be
        # worse than not firing: it teaches people the alert is noise.
        logger.warning("Capability alert %s has no usable spec limits — skipping.", alert.pk)
        return None

    return Outcome(
        value=float(cpk),
        subject="Cpk",
        summary=(f"Cpk for {alert.column} is {cpk:.3g} "
                 f"({out.get('pct_out_of_spec', 0):.2f}% out of spec, "
                 f"{'capable' if out.get('capable') else 'NOT capable'})."),
        detail={"cpk": cpk, "cp": out.get("cp"), "ppk": out.get("ppk"),
                "pct_out_of_spec": out.get("pct_out_of_spec"),
                "dpmo": out.get("dpmo"), "lsl": out.get("lsl"), "usl": out.get("usl")},
    )


# --------------------------------------------------------------------------
# Anomaly — "alert me when anomalies appear"
# --------------------------------------------------------------------------
def anomaly_rule(alert, df) -> Outcome | None:
    from analytics import anomaly
    from analytics.engine import AnalyticsError

    cfg = dict(alert.config or {})
    scope = cfg.pop("scope", "univariate")
    kwargs = {"scope": scope, **cfg}
    if scope in ("univariate", "series"):
        kwargs.setdefault("value_column", alert.column)
    else:
        kwargs.setdefault("columns", cfg.get("columns") or [alert.column])

    try:
        out = anomaly.detect(df, **kwargs)
    except AnalyticsError as exc:
        logger.warning("Anomaly alert %s could not run: %s", alert.pk, exc)
        return None

    found = out.get("anomalies") or []
    n = int(out.get("anomaly_count", len(found)))
    return Outcome(
        value=float(n),
        subject="anomalies",
        summary=(f"{n} anomal{'y' if n == 1 else 'ies'} detected in {alert.column} "
                 f"({out.get('method', scope)})." if n else
                 f"No anomalies detected in {alert.column}."),
        detail={"method": out.get("method"), "scope": scope, "anomaly_count": n,
                "anomalies": found[:20]},
    )


# --------------------------------------------------------------------------
# Forecast breach — "alert me BEFORE it goes off-spec"
# --------------------------------------------------------------------------
def forecast_rule(alert, df) -> Outcome | None:
    from analytics import predict
    from analytics.engine import AnalyticsError

    cfg = dict(alert.config or {})
    periods = int(cfg.get("periods", 6))
    try:
        out = predict.forecast(df, alert.column, periods=periods)
    except AnalyticsError as exc:
        logger.warning("Forecast alert %s could not run: %s", alert.pk, exc)
        return None

    values = [p.get("value") if isinstance(p, dict) else p
              for p in (out.get("forecast") or [])]
    values = [float(v) for v in values if v is not None]
    if not values:
        return None

    # Take the extreme in the direction the alert cares about, so the question is
    # "will it breach within the horizon?" — not "where does it land at the end?",
    # which would miss a breach that recovers before the last period.
    upward = alert.condition in ("gt", "gte")
    value = max(values) if upward else min(values)

    # `trend` is a dict. This summary becomes the body of an EMAIL, so render it —
    # a raw {'slope': 0.12, 'intercept': 1.0, …} in someone's inbox is not a message.
    trend = out.get("trend") or {}
    bits = []
    if isinstance(trend, dict):
        if trend.get("direction"):
            bits.append(str(trend["direction"]))
        if trend.get("slope") is not None:
            bits.append(f"{trend['slope']:+.4g} per period")
    trend_text = f" (trend: {', '.join(bits)})" if bits else ""

    current = float(values[0])
    return Outcome(
        value=value,
        subject=f"projected {alert.column}",
        summary=(f"{alert.column} is projected to reach {value:.4g} within "
                 f"{periods} period(s){trend_text}. It has not breached yet — "
                 f"the next projected value is {current:.4g}."),
        detail={"periods": periods, "forecast": values[:24],
                "trend": trend, "direction": "max" if upward else "min"},
    )


RULES = {
    "spc": spc_rule,
    "capability": capability_rule,
    "anomaly": anomaly_rule,
    "forecast": forecast_rule,
}
