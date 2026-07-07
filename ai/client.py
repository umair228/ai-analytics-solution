"""Provider-agnostic AI client — chat, insights and widget suggestions.

Generation flows through a pluggable provider (see ``ai/providers``), selected
by ``settings.LLM_PROVIDER`` ("anthropic" cloud or "local" on-prem). The public
functions keep stable signatures + return types, so callers (``ai.views``,
``docsearch.answer``, ``forecasting.views.insights``) and the frontend response
shapes are unchanged regardless of which provider is active.
"""
import json

from .providers import AINotConfigured, get_provider

__all__ = [
    "AINotConfigured",
    "is_configured",
    "active_model",
    "run_chat",
    "generate_insights",
    "suggest_widgets",
    "suggest_dataset_filters",
]

SYSTEM_PROMPT = (
    "You are DSE Assistant, an AI analytics co-pilot embedded in Data Science Engine "
    "(DSE) — AI-Powered Insights for Modern Laboratories, an analytics platform for LabWare LIMS "
    "and laboratory data.\n\n"
    "You help users understand and interpret their data: answering questions, "
    "explaining trends, flagging anomalies and suggesting useful analyses. When "
    "a dataset is supplied, ground every answer in the actual columns, "
    "statistics and sample rows provided — never invent numbers, and if the "
    "data cannot answer a question, say so plainly. Be concise, specific and "
    "practical. Prefer short paragraphs and bullet points; avoid heavy markdown "
    "since answers render as plain text."
)

_INSIGHTS_PROMPT = (
    "Analyse the dataset above and write a concise insights brief for a "
    "laboratory operations manager. Use these short labelled sections:\n"
    "1. Headline — the single most important takeaway.\n"
    "2. Trends & patterns — what the data shows.\n"
    "3. Anomalies — anything unusual or worth attention.\n"
    "4. Recommended next analyses — two or three concrete suggestions.\n\n"
    "Ground every point in the actual statistics and sample rows provided. "
    "Keep the whole brief under about 280 words."
)

_WIDGET_PROMPT = (
    "Based on the dataset above, suggest 4-5 useful dashboard widgets. "
    'Return a JSON object of the exact form {"widgets": [ ... ]} and nothing else. '
    "Each item in the array must have these keys:\n"
    "  widget_type    — one of: bar, line, pie, kpi, table, scatter, area\n"
    "  title          — short descriptive title\n"
    "  category_field — best column for the X axis or grouping (exact column name)\n"
    "  value_field    — best column for the Y axis or KPI value (exact column name)\n"
    "  rollup         — one of: sum, count, avg, min, max\n"
    "  reason         — one sentence explaining the value of this chart\n"
    "Use only exact column names from the dataset. Output JSON only — no markdown."
)


def _system_blocks(dataset_context):
    """Build the system prompt. The stable prefix is marked for prompt caching
    so multi-turn conversations reuse it cheaply (used by the Anthropic provider;
    the local provider does prefix caching server-side and ignores the marker)."""
    blocks = [{"type": "text", "text": SYSTEM_PROMPT}]
    if dataset_context:
        blocks.append({
            "type": "text",
            "text": "The user is working with this dataset:\n\n" + dataset_context,
        })
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def is_configured() -> bool:
    """Whether the active provider is configured (drives the 503/extractive gates)."""
    return get_provider().configured


def active_model():
    """Model name to surface in status endpoints (None if unconfigured)."""
    provider = get_provider()
    return provider.model_name if provider.configured else None


def run_chat(messages, dataset_context=None) -> str:
    """Run a multi-turn chat. ``messages`` is a list of {role, content} dicts."""
    provider = get_provider()
    if not provider.configured:
        raise AINotConfigured(provider.not_configured_message())
    text = provider.chat(
        system_blocks=_system_blocks(dataset_context),
        messages=messages,
        max_tokens=2048,
    )
    return text or "(no response)"


def generate_insights(dataset_context) -> str:
    """Generate a natural-language insights brief for a dataset."""
    provider = get_provider()
    if not provider.configured:
        raise AINotConfigured(provider.not_configured_message())
    text = provider.chat(
        system_blocks=_system_blocks(dataset_context),
        messages=[{"role": "user", "content": _INSIGHTS_PROMPT}],
        max_tokens=2048,
    )
    return text or "(no insights generated)"


def suggest_widgets(dataset_context) -> list:
    """Suggest dashboard widget configurations for a dataset. Returns a list of
    widget-config dicts (the shape the frontend expects under "suggestions")."""
    provider = get_provider()
    if not provider.configured:
        raise AINotConfigured(provider.not_configured_message())
    prompt = f"{dataset_context}\n\n{_WIDGET_PROMPT}"
    text = provider.chat(
        system_blocks=_system_blocks(None),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900,
        response_format="json",
    )
    return _parse_widgets(text)


_FILTERS_PROMPT = (
    "The candidate filters below were derived from the dataset's columns. "
    "Pick the 3-6 that would be MOST useful for exploring this dataset — the "
    "ones an analyst would actually reach for first — and order them by "
    "usefulness. For each, write a one-sentence reason grounded in this "
    "specific dataset (mention what slicing by it reveals).\n\n"
    "CANDIDATE FILTERS (JSON):\n{candidates}\n\n"
    'Return a JSON object of the exact form {{"filters": [ ... ]}} and nothing '
    "else. Each item must have keys: column (exact name from the candidates), "
    "type (copy it from the candidate), reason. Output JSON only — no markdown."
)


def suggest_dataset_filters(dataset_context, candidates) -> list:
    """Select and explain the most useful filters for a dataset. Returns a
    list of {column, type, reason} dicts drawn from ``candidates``."""
    provider = get_provider()
    if not provider.configured:
        raise AINotConfigured(provider.not_configured_message())
    slim = [{k: c[k] for k in ("column", "type", "reason") if k in c}
            for c in candidates]
    prompt = (f"{dataset_context}\n\n"
              + _FILTERS_PROMPT.format(candidates=json.dumps(slim)))
    text = provider.chat(
        system_blocks=_system_blocks(None),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=700,
        response_format="json",
    )
    return _parse_list(text, ("filters", "suggestions"))


def _parse_widgets(text) -> list:
    """Robustly pull the widget list out of a model response, tolerating JSON
    objects ({"widgets":[...]}), bare arrays, and markdown-fenced output."""
    return _parse_list(text, ("widgets", "suggestions"))


def _parse_list(text, keys) -> list:
    """Pull a list out of a model response: a JSON object keyed by any of
    ``keys``, a bare array, or either embedded in markdown-fenced output."""
    text = (text or "").strip()

    def from_obj(data):
        for k in keys:
            if isinstance(data, dict) and data.get(k):
                return data[k]
        return []

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return from_obj(data)
        if isinstance(data, list):
            return data
    except Exception:  # noqa: BLE001
        pass
    # Fallback: a JSON array substring.
    start, end = text.find("["), text.rfind("]") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except Exception:  # noqa: BLE001
            pass
    # Fallback: a JSON object substring.
    start, end = text.find("{"), text.rfind("}") + 1
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end])
            if isinstance(data, dict):
                return from_obj(data)
        except Exception:  # noqa: BLE001
            pass
    return []
