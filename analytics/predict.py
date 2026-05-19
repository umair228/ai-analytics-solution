"""Predictive analytics — forecasting, trend analysis, anomaly detection and
auto-summary. Classical statistics only (numpy / pandas), no external models.
"""
import numpy as np
import pandas as pd

from .engine import AnalyticsError, _num


def _series(df, column):
    if not column or column not in df.columns:
        raise AnalyticsError("A valid numeric 'value_column' is required.")
    series = pd.to_numeric(df[column], errors="coerce").dropna()
    return series.to_numpy(dtype=float)


def _holt(y, alpha=0.5, beta=0.3):
    """Holt's linear (double exponential) smoothing — returns level, trend, fit."""
    level = float(y[0])
    trend = float(y[1] - y[0]) if len(y) > 1 else 0.0
    fitted = [level]
    for value in y[1:]:
        prev_level = level
        level = alpha * value + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
        fitted.append(level)
    return level, trend, fitted


# --------------------------------------------------------------------------
# Forecasting + trend
# --------------------------------------------------------------------------
def forecast(df, value_column, periods=6):
    """Forecast the next `periods` points of a numeric series, with a trend
    summary and an approximate confidence band."""
    y = _series(df, value_column)
    n = len(y)
    if n < 3:
        raise AnalyticsError("Need at least 3 numeric points to forecast.")

    # Linear-regression trend
    x = np.arange(n)
    slope, intercept = np.polyfit(x, y, 1)
    fitted_line = slope * x + intercept
    ss_res = float(np.sum((y - fitted_line) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = (1 - ss_res / ss_tot) if ss_tot else 1.0

    # Holt's linear forecast
    level, trend, _ = _holt(y)
    projection = [level + trend * (h + 1) for h in range(periods)]

    band = 1.96 * float(np.std(y - fitted_line))
    direction = (
        "increasing" if slope > 1e-9 else "decreasing" if slope < -1e-9 else "flat"
    )
    pct_change = float((y[-1] - y[0]) / abs(y[0]) * 100) if y[0] else 0.0

    return {
        "value_column": value_column,
        "periods": periods,
        "points": n,
        "history": [_num(v) for v in y],
        "forecast": [_num(v) for v in projection],
        "lower": [_num(v - band) for v in projection],
        "upper": [_num(v + band) for v in projection],
        "trend": {
            "slope": _num(slope),
            "intercept": _num(intercept),
            "r_squared": _num(r_squared),
            "direction": direction,
            "pct_change": _num(pct_change),
            "next_value": _num(projection[0]) if projection else None,
        },
    }


# --------------------------------------------------------------------------
# Anomaly / outlier detection
# --------------------------------------------------------------------------
def detect_anomalies(df, value_column, method="zscore", threshold=3.0):
    """Flag outliers in a numeric series via z-score or the IQR rule."""
    y = _series(df, value_column)
    n = len(y)
    if n < 3:
        raise AnalyticsError("Need at least 3 numeric points for anomaly detection.")

    method = (method or "zscore").lower()
    if method == "iqr":
        q1, q3 = np.percentile(y, [25, 75])
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        flags = (y < lower) | (y > upper)
        scores = [None] * n
        bounds = {"lower": _num(lower), "upper": _num(upper)}
    else:
        method = "zscore"
        mean = float(y.mean())
        std = float(y.std())
        z = (y - mean) / std if std else np.zeros(n)
        flags = np.abs(z) > threshold
        scores = [_num(v) for v in z]
        bounds = {
            "mean": _num(mean),
            "std": _num(std),
            "lower": _num(mean - threshold * std),
            "upper": _num(mean + threshold * std),
        }

    anomalies = [
        {"index": int(i), "value": _num(y[i]), "score": scores[i]}
        for i in range(n)
        if flags[i]
    ]
    return {
        "value_column": value_column,
        "method": method,
        "threshold": threshold,
        "total": n,
        "anomaly_count": len(anomalies),
        "values": [_num(v) for v in y],
        "flags": [bool(f) for f in flags],
        "anomalies": anomalies,
        "bounds": bounds,
    }


# --------------------------------------------------------------------------
# Intelligent auto-summary
# --------------------------------------------------------------------------
def auto_summary(df):
    """Produce per-column insights — trend direction, mean and outlier count."""
    insights = []
    n = len(df)
    for col in df.columns:
        numeric = pd.to_numeric(df[col], errors="coerce")
        if n == 0 or numeric.notna().sum() < max(3, n * 0.6):
            continue
        y = numeric.dropna().to_numpy(dtype=float)
        m = len(y)
        if m < 3:
            continue

        slope = float(np.polyfit(np.arange(m), y, 1)[0])
        direction = (
            "rising" if slope > 1e-9 else "falling" if slope < -1e-9 else "flat"
        )
        mean = float(y.mean())
        std = float(y.std())
        z = np.abs((y - mean) / std) if std else np.zeros(m)
        outliers = int(np.sum(z > 3))
        pct_change = float((y[-1] - y[0]) / abs(y[0]) * 100) if y[0] else 0.0

        text = f"{col} is {direction}"
        if direction != "flat":
            text += f" ({pct_change:+.1f}% across the dataset)"
        text += f"; mean {mean:.2f}"
        if outliers:
            text += f", {outliers} outlier(s) detected"
        text += "."

        insights.append({
            "column": str(col),
            "direction": direction,
            "mean": _num(mean),
            "std": _num(std),
            "pct_change": _num(pct_change),
            "outliers": outliers,
            "text": text,
        })

    return {
        "row_count": n,
        "numeric_columns": len(insights),
        "insights": insights,
    }
