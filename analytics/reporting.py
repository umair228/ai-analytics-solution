"""Adapters that turn a saved :class:`AnalysisRun` (or a chart config) into the
``AnswerEnvelope`` shape that :func:`ai.exporters.export` understands, so saved
runs and charts can be emailed/scheduled through the existing report pipeline.
"""
from __future__ import annotations


def _flatten_forecast(result: dict):
    chosen = result.get("forecast") or {}
    hist = chosen.get("history") or []
    fc = chosen.get("forecast") or []
    lo = chosen.get("lower") or []
    up = chosen.get("upper") or []
    hlabels = result.get("history_labels") or [f"h{i + 1}" for i in range(len(hist))]
    flabels = result.get("forecast_labels") or [f"f{i + 1}" for i in range(len(fc))]
    columns = ["period", "type", "value", "lower", "upper"]
    rows = [[hlabels[i] if i < len(hlabels) else i, "history", hist[i], None, None]
            for i in range(len(hist))]
    rows += [[flabels[i] if i < len(flabels) else i, "forecast", fc[i],
              lo[i] if i < len(lo) else None, up[i] if i < len(up) else None]
             for i in range(len(fc))]
    return columns, rows, {"type": "line", "category_field": "period", "value_field": "value"}


def _flatten_anomaly(result: dict):
    anoms = result.get("anomalies") or []
    columns = ["row", "score"]
    rows = [[a.get("row", a.get("index")), a.get("score")] for a in anoms if isinstance(a, dict)]
    return columns, rows, None


def _flatten_statistics(result: dict):
    if isinstance(result.get("rows"), list) and isinstance(result.get("columns"), list):
        cols = result["columns"]
        rows = [[r.get(c) for c in cols] for r in result["rows"] if isinstance(r, dict)]
        return cols, rows, None
    columns = ["metric", "value"]
    rows = [[k, v] for k, v in result.items()
            if isinstance(v, (int, float, str, bool)) and k != "interpretation"]
    return columns, rows, None


_FLATTENERS = {
    "forecast": _flatten_forecast,
    "anomaly": _flatten_anomaly,
    "statistics": _flatten_statistics,
}


def run_to_envelope(run) -> dict:
    """Build an AnswerEnvelope from a saved AnalysisRun."""
    result = run.result or {}
    flatten = _FLATTENERS.get(run.workflow, _flatten_statistics)
    columns, rows, chart = flatten(result)
    return {
        "narrative": result.get("interpretation") or run.name or "",
        "result": {"columns": columns, "rows": rows, "row_count": len(rows)},
        "chart": chart,
        "provenance": {
            "path": f"AnalysisRun #{run.id} ({run.workflow}/{run.method})",
            "model": run.method,
            "generated_at": run.created_at.isoformat() if run.created_at else None,
        },
    }


def chart_to_envelope(chart_config: dict) -> dict:
    """Build an AnswerEnvelope from a saved chart config.

    ``chart_config`` is expected to carry ``columns`` + ``rows`` (the chart's
    underlying data) and a ``type`` / ``category_field`` / ``value_field`` spec.
    """
    cc = chart_config or {}
    columns = cc.get("columns") or []
    rows = cc.get("rows") or []
    chart = None
    if cc.get("type"):
        chart = {"type": cc.get("type"),
                 "category_field": cc.get("category_field"),
                 "value_field": cc.get("value_field")}
    return {
        "narrative": cc.get("title") or cc.get("narrative") or "",
        "result": {"columns": columns, "rows": rows, "row_count": len(rows)},
        "chart": chart,
        "provenance": {"path": "Chart Studio export"},
    }
