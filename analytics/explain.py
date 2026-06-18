"""Plain-English explanations of analytics results via the local LLM.

Modeled on :mod:`forecasting.views.insights`: we build a compact, numbers-
grounded *context* from a result dict (never the raw JSON dump), then ask the
configured provider (:func:`ai.client.run_chat`) to narrate it for a lab-ops
audience. Every entry point degrades gracefully — if the LLM is not configured
or errors, we return a structured payload with ``configured: False`` and never
raise, so an explanation can never break the underlying analysis.
"""
from __future__ import annotations

from ai.client import AINotConfigured, is_configured, run_chat

# One narration brief per workflow (BRIEF_PROMPT-style).
_PROMPTS = {
    "anomaly": (
        "Explain these anomaly-detection results for a laboratory operations "
        "manager. Cover: how many points were flagged and how unusual they are; "
        "which variables drove the flags (use the per-feature contributions if "
        "present); whether this looks like noise, drift or a real excursion; and "
        "2-3 concrete things to check. Keep it under ~200 words."
    ),
    "forecast": (
        "Explain this forecast for a laboratory operations manager. Cover: the "
        "trajectory (rising/falling/flat, magnitude, any seasonality); which "
        "method was selected and why it is trustworthy (cite the backtest "
        "accuracy, e.g. sMAPE/MAPE); the main risks or watch-outs; and 2-3 "
        "recommended actions. Keep it under ~200 words."
    ),
    "statistics": (
        "Explain this statistical result in plain English for a non-statistician. "
        "State clearly whether the effect is significant and what that means in "
        "practice, report the effect size and how large it is, note any assumption "
        "warnings, and give a one-line bottom-line. Keep it under ~180 words."
    ),
    "generic": (
        "Summarise this analytics result for a laboratory operations manager in "
        "plain English, grounded strictly in the numbers above. Keep it concise."
    ),
}

_SUGGESTIONS = {
    "anomaly": [
        "Which records are the most extreme and why?",
        "Could these anomalies be measurement error rather than real?",
        "What should we investigate first?",
    ],
    "forecast": [
        "How confident should we be in this forecast?",
        "What would change the outlook?",
        "Which method was most accurate and why?",
    ],
    "statistics": [
        "Is this result practically meaningful, not just significant?",
        "What are the caveats or assumptions?",
        "What analysis should we run next?",
    ],
    "generic": ["What stands out most here?", "What should we do next?"],
}


def _fmt(value, n=4):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{f:.{n}g}"


def _lines_anomaly(result: dict) -> list[str]:
    out = [
        f"Method: {result.get('test') or result.get('method')}",
        f"Rows analysed: {result.get('n')}; flagged: {result.get('anomaly_count')}",
    ]
    if result.get("contamination") is not None:
        out.append(f"Contamination setting: {result['contamination']}")
    flagged = result.get("anomalies") or result.get("flags") or []
    for rec in flagged[:8]:
        if not isinstance(rec, dict):
            continue
        bits = [f"row {rec.get('row', rec.get('index'))}"]
        if rec.get("score") is not None:
            bits.append(f"score={_fmt(rec['score'])}")
        contrib = rec.get("contributions") or rec.get("feature_contributions")
        if isinstance(contrib, dict) and contrib:
            top = sorted(contrib.items(), key=lambda kv: -abs(float(kv[1])))[:3]
            bits.append("drivers: " + ", ".join(f"{k}={_fmt(v)}" for k, v in top))
        elif isinstance(rec.get("values"), dict):
            vals = list(rec["values"].items())[:4]
            bits.append("values: " + ", ".join(f"{k}={_fmt(v)}" for k, v in vals))
        out.append("  - " + "; ".join(bits))
    return out


def _lines_forecast(result: dict) -> list[str]:
    out = [
        f"Selected method: {result.get('selected')}",
        f"Seasonality (period): {result.get('season')}",
        f"Horizon: {result.get('periods')} periods; metric: {result.get('metric')}",
    ]
    scores = result.get("scores") or {}
    if isinstance(scores, dict) and scores:
        out.append("Backtest accuracy (lower is better):")
        ranked = sorted(
            ((m, s) for m, s in scores.items() if isinstance(s, dict) and s.get("ok")),
            key=lambda kv: kv[1].get("smape", float("inf")),
        )[:6]
        for m, s in ranked:
            out.append(
                f"  - {m}: sMAPE={_fmt(s.get('smape'))}, MAPE={_fmt(s.get('mape'))}, "
                f"RMSE={_fmt(s.get('rmse'))}, MAE={_fmt(s.get('mae'))}"
            )
    chosen = result.get("forecast") or {}
    hist = chosen.get("history") or result.get("history") or []
    fc = chosen.get("forecast") or result.get("forecast") or []
    if isinstance(hist, list) and hist and isinstance(hist[0], (int, float)):
        out.append("Recent history: " + ", ".join(_fmt(v) for v in hist[-6:]))
    if isinstance(fc, list) and fc and isinstance(fc[0], (int, float)):
        out.append("Forecast: " + ", ".join(_fmt(v) for v in fc[:8]))
    trend = chosen.get("trend") or result.get("trend") or {}
    if isinstance(trend, dict) and trend:
        out.append(
            f"Trend: {trend.get('direction')} (slope {_fmt(trend.get('slope'))}, "
            f"R²={_fmt(trend.get('r_squared'))})"
        )
    return out


def _lines_statistics(result: dict) -> list[str]:
    out = [f"Test: {result.get('test')}"]
    for key in ("statistic", "t_statistic", "f_statistic", "chi2_statistic",
                "coefficient", "r_squared", "p_value", "significant", "is_normal",
                "cohens_d", "eta_squared", "cramers_v", "cp", "cpk", "capable",
                "direction", "mann_kendall"):
        if key in result and not isinstance(result[key], (list, dict)):
            out.append(f"{key}: {_fmt(result[key]) if isinstance(result[key], (int, float)) else result[key]}")
    assumptions = result.get("assumptions")
    if isinstance(assumptions, dict):
        if assumptions.get("recommended"):
            out.append(f"Recommended approach: {assumptions['recommended']}")
        for w in (assumptions.get("warnings") or [])[:3]:
            out.append(f"Assumption note: {w}")
    if result.get("warning"):
        out.append(f"Warning: {result['warning']}")
    return out


_BUILDERS = {
    "anomaly": _lines_anomaly,
    "forecast": _lines_forecast,
    "statistics": _lines_statistics,
}


def build_context(workflow: str, result: dict, dataset=None) -> str:
    """A compact, numbers-grounded textual summary of a result dict."""
    header = []
    if dataset is not None:
        header.append(f"Dataset: {getattr(dataset, 'name', '')}")
        cols = getattr(dataset, "cached_columns", None)
        if cols:
            header.append("Columns: " + ", ".join(str(c) for c in cols[:30]))
    builder = _BUILDERS.get(workflow)
    body = builder(result) if (builder and isinstance(result, dict)) else []
    if not body and isinstance(result, dict) and result.get("interpretation"):
        body = [str(result["interpretation"])]
    text = "\n".join(header + ["", f"{workflow.title()} result:"] + body)
    return text[:6000]


def explain_result(workflow: str, result: dict, *, dataset=None, question: str = "") -> dict:
    """Return ``{configured, answer, suggestions}``. Never raises."""
    workflow = (workflow or "generic").lower()
    suggestions = _SUGGESTIONS.get(workflow, _SUGGESTIONS["generic"])
    if not is_configured():
        return {"configured": False,
                "answer": "AI explanation is unavailable — the local model is not configured.",
                "suggestions": suggestions}
    try:
        context = build_context(workflow, result, dataset)
        if question:
            user_msg = (
                "Answer the user's question grounded strictly in the result above. "
                "If the result cannot answer it, say so.\n\nQuestion: " + question
            )
        else:
            user_msg = _PROMPTS.get(workflow, _PROMPTS["generic"])
        answer = run_chat([{"role": "user", "content": user_msg}], dataset_context=context)
        return {"configured": True, "answer": answer, "suggestions": suggestions}
    except AINotConfigured:
        return {"configured": False,
                "answer": "AI explanation is not configured.",
                "suggestions": suggestions}
    except Exception as exc:  # noqa: BLE001 - explanation must never break a request
        return {"configured": True,
                "answer": f"Could not generate an explanation: {exc}",
                "suggestions": suggestions}
