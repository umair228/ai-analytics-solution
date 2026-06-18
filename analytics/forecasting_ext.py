"""Expanded forecasting (Phase 4) — forecast ANY operational metric, not just
lab results.

The MOM asks to extend forecasting to personnel/workforce, time/productivity and
cost/resource metrics. One general abstraction covers all of them: take a dataset
with a date column, build a time series (resample by day/week/month/quarter/year
with an aggregation, optionally per group), and project it forward.

Examples on the LIMS warehouse:
  * Personnel/workload — group_by=ANALYST, agg=count, freq=M  → samples/analyst/month
  * Time/productivity  — value=TAT_HOURS, agg=mean,  freq=W  → avg turnaround trend
  * Cost/resource      — value=cost,      agg=sum,   freq=M  → monthly cost trend

Projection reuses the tested linear-trend + Holt logic in :mod:`analytics.predict`,
so no extra heavy model dependency (NeuralProphet stays for the dedicated LIMS
forecasts in the ``forecasting`` app).
"""
from __future__ import annotations

import pandas as pd

from . import predict
from .engine import AnalyticsError, _num

# user-facing freq → (pandas resample rule, label format)
_FREQ = {
    "D": ("D", "%Y-%m-%d"),
    "W": ("W", "%Y-%m-%d"),
    "M": ("ME", "%Y-%m"),
    "Q": ("QE", "%Y-Q%q"),
    "Y": ("YE", "%Y"),
}
_AGGS = {"count", "sum", "mean", "avg", "min", "max", "median"}
MAX_GROUPS = 8


def _label(ts, fmt):
    if "%q" in fmt:  # pandas has no %q
        return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"
    return ts.strftime(fmt)


def _build_series(sub, rule, value_column, agg):
    g = sub.set_index("_dt").sort_index()
    if agg == "count":
        s = g.resample(rule).size()
    else:
        if not value_column or value_column not in sub.columns:
            raise AnalyticsError("A numeric 'value_column' is required for this aggregation.")
        vals = pd.to_numeric(g[value_column], errors="coerce")
        s = vals.resample(rule).agg("mean" if agg == "avg" else agg)
    return s.dropna()


def _project(s, rule, fmt, periods, method=None):
    if len(s) < 3:
        return None
    y = s.to_numpy(float)
    future = pd.date_range(s.index[-1], periods=periods + 1, freq=rule)[1:]
    hist = [{"period": _label(ts, fmt), "value": _num(float(v))} for ts, v in s.items()]

    # Advanced engine path: a specific method (or "auto") routes through the
    # unified forecasting engine; falls back to Holt if it is unavailable.
    if method and method not in ("holt",):
        try:
            from .forecasting_models import run_forecast
            res = run_forecast(y, periods=periods, methods=method, freq=rule[0])
            chosen = res.get("forecast")
            if chosen:
                fcv = chosen.get("forecast") or []
                lo, up = chosen.get("lower"), chosen.get("upper")
                return {
                    "points": len(s), "history": hist,
                    "forecast": [{"period": _label(ts, fmt),
                                  "value": fcv[i] if i < len(fcv) else None,
                                  "lower": (lo[i] if lo and i < len(lo) else None),
                                  "upper": (up[i] if up and i < len(up) else None)}
                                 for i, ts in enumerate(future)],
                    "trend": chosen.get("trend") or {},
                    "method": res.get("selected"),
                    "selected": res.get("selected"),
                    "scores": res.get("scores"),
                }
        except Exception:  # noqa: BLE001 - degrade to the Holt projection
            pass

    fc = predict.forecast(pd.DataFrame({"v": y}), "v", periods=periods)
    return {
        "points": len(s),
        "history": hist,
        "forecast": [{"period": _label(ts, fmt), "value": fc["forecast"][i],
                      "lower": fc["lower"][i], "upper": fc["upper"][i]}
                     for i, ts in enumerate(future)],
        "trend": fc["trend"],
    }


def forecast_metric(df, date_column, value_column=None, agg="count", freq="M",
                    periods=6, group_by=None, method=None, exog_columns=None):
    """Forecast an operational metric over time.

    Resamples by ``freq`` (D/W/M/Q/Y), aggregating ``value_column`` with ``agg``
    (or counting rows when agg='count'); optionally one series per ``group_by``
    value (top groups by volume). Returns history + forecast + trend per series.

    ``method``/``exog_columns`` select the unified forecasting engine
    (:mod:`analytics.forecasting_models`); when ``method`` is None/"holt" the
    historic Holt's-linear projection is used.
    """
    _ = exog_columns  # exog regressors are handled by the /analytics/forecast/ workbench
    if date_column not in df.columns:
        raise AnalyticsError(f"Date column '{date_column}' not found.")
    agg = (agg or "count").lower()
    if agg not in _AGGS:
        raise AnalyticsError(f"Unsupported aggregation '{agg}'.")
    freq = (freq or "M").upper()
    if freq not in _FREQ:
        raise AnalyticsError("freq must be one of D, W, M, Q, Y.")
    rule, fmt = _FREQ[freq]
    periods = int(min(max(periods or 6, 1), 60))

    work = df.copy()
    work["_dt"] = pd.to_datetime(work[date_column], errors="coerce")
    work = work.dropna(subset=["_dt"])
    if work.empty:
        raise AnalyticsError(f"Column '{date_column}' has no parseable dates.")

    metric = f"{agg} of {value_column}" if value_column and agg != "count" else "row count"
    base = {
        "test": "forecast_metric", "metric": metric, "date_column": date_column,
        "value_column": value_column, "aggregation": agg, "frequency": freq,
        "periods": periods,
    }

    if group_by:
        if group_by not in work.columns:
            raise AnalyticsError(f"group_by column '{group_by}' not found.")
        top = work[group_by].astype(str).value_counts().head(MAX_GROUPS).index
        series_list = []
        for name in top:
            sub = work[work[group_by].astype(str) == name]
            proj = _project(_build_series(sub, rule, value_column, agg), rule, fmt, periods, method)
            if proj:
                series_list.append({"group": str(name), **proj})
        if not series_list:
            raise AnalyticsError("Not enough history per group (need ≥3 periods).")
        rising = [s for s in series_list if s["trend"]["direction"] == "increasing"]
        return {
            **base, "group_by": group_by, "series_count": len(series_list),
            "series": series_list,
            "interpretation": (
                f"Forecast {metric} by {group_by} ({freq}, next {periods}). "
                f"{len(rising)}/{len(series_list)} group(s) trending up. "
                + (f"Fastest-rising: {max(series_list, key=lambda s: s['trend']['slope'])['group']}."
                   if series_list else "")
            ),
        }

    s = _build_series(work, rule, value_column, agg)
    proj = _project(s, rule, fmt, periods, method)
    if proj is None:
        raise AnalyticsError("Not enough history to forecast (need ≥3 periods).")
    t = proj["trend"]
    return {
        **base, **proj,
        "interpretation": (
            f"{metric} is {t['direction']} (slope {t['slope']}/{freq}, "
            f"R²={t['r_squared']}). Next {freq}: ~{t['next_value']}."
        ),
    }
