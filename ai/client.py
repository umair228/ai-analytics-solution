"""Claude API integration — chat and insight generation with prompt caching."""
from django.conf import settings

SYSTEM_PROMPT = (
    "You are DSE Assistant, an AI analytics co-pilot embedded in Data Science Engine "
    "(DSE) — an AI-Powered Interactive & Agentic Analytics platform for LabWare LIMS "
    "and laboratory data.\n\n"
    "You help users understand and interpret their data: answering questions, "
    "explaining trends, flagging anomalies and suggesting useful analyses. When "
    "a dataset is supplied, ground every answer in the actual columns, "
    "statistics and sample rows provided — never invent numbers, and if the "
    "data cannot answer a question, say so plainly. Be concise, specific and "
    "practical. Prefer short paragraphs and bullet points; avoid heavy markdown "
    "since answers render as plain text."
)


class AINotConfigured(Exception):
    """Raised when the Claude API key is not configured."""


_client = None


def is_configured() -> bool:
    return bool(settings.CLAUDE_API_KEY)


def _get_client():
    global _client
    if not is_configured():
        raise AINotConfigured(
            "The Claude API key is not configured. "
            "Set CLAUDE_API_KEY in the backend .env."
        )
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=settings.CLAUDE_API_KEY)
    return _client


def _system_blocks(dataset_context):
    """Build the system prompt. The stable prefix is marked for prompt caching
    so multi-turn conversations reuse it cheaply."""
    blocks = [{"type": "text", "text": SYSTEM_PROMPT}]
    if dataset_context:
        blocks.append({
            "type": "text",
            "text": "The user is working with this dataset:\n\n" + dataset_context,
        })
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _text(response) -> str:
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def run_chat(messages, dataset_context=None) -> str:
    """Run a multi-turn chat. ``messages`` is a list of {role, content} dicts."""
    client = _get_client()
    response = client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=2048,
        system=_system_blocks(dataset_context),
        messages=messages,
    )
    return _text(response) or "(no response)"


def generate_insights(dataset_context) -> str:
    """Generate a natural-language insights brief for a dataset."""
    client = _get_client()
    prompt = (
        "Analyse the dataset above and write a concise insights brief for a "
        "laboratory operations manager. Use these short labelled sections:\n"
        "1. Headline — the single most important takeaway.\n"
        "2. Trends & patterns — what the data shows.\n"
        "3. Anomalies — anything unusual or worth attention.\n"
        "4. Recommended next analyses — two or three concrete suggestions.\n\n"
        "Ground every point in the actual statistics and sample rows provided. "
        "Keep the whole brief under about 280 words."
    )
    response = client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=2048,
        system=_system_blocks(dataset_context),
        messages=[{"role": "user", "content": prompt}],
    )
    return _text(response) or "(no insights generated)"
