"""LLM SQL generation — provider-agnostic via :mod:`ai.providers`.

Uses the same configured model as the rest of DIS (``LLM_PROVIDER``), so it runs
on Claude (cloud) or a local Qwen (Ollama/vLLM) with no code change. Asks for a
single read-only statement and nothing else; :func:`sql_tools.extract_sql` cleans
up any stray prose/fences the model adds anyway.
"""
from __future__ import annotations

from ai.providers import AINotConfigured, get_provider

from .sql_tools import dialect_label

_SYSTEM = (
    "You are a meticulous data analyst who answers a question by writing exactly ONE "
    "read-only SQL query for the target database.\n"
    "Target dialect: {dialect}.\n"
    "Hard rules:\n"
    "- Use ONLY the tables and columns listed below. NEVER invent a table or column name.\n"
    "- Apply the DATA NOTES exactly (value encodings like IN_SPEC='F', required casts, joins).\n"
    "- Write a single SELECT or WITH...SELECT. No INSERT/UPDATE/DELETE/DDL, no multiple statements.\n"
    "- Aggregate and ORDER so the answer the user wants is the first row(s).\n"
    "- Output ONLY the SQL. No explanation, no comments, no markdown fences.\n\n"
    "{schema}"
)


def generate_sql(question, schema_context, source_type, *,
                 error_feedback=None, prior_sql=None, max_tokens=600) -> str:
    """Return raw model text (clean it with sql_tools.extract_sql). Raises
    :class:`ai.providers.AINotConfigured` if no LLM endpoint is set up."""
    provider = get_provider()
    if not provider.configured:
        raise AINotConfigured(provider.not_configured_message())

    system = _SYSTEM.format(dialect=dialect_label(source_type), schema=schema_context)
    user = question
    if error_feedback:
        user = (
            f"{question}\n\n"
            "Your previous SQL did not work — fix it and return only the corrected SQL.\n"
            f"Previous SQL:\n{prior_sql or ''}\n"
            f"Database error:\n{error_feedback}"
        )
    return provider.chat(
        system_blocks=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user}],
        max_tokens=max_tokens,
    )
