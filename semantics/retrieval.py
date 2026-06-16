"""Schema-RAG: pick the handful of tables relevant to a question out of a large
curated layer, then pull in their certified-join partners so the join targets are
present (DIS_TalkToData_Plan.md §4). Lexical scoring over curated names +
descriptions + synonyms + column metadata; an embedding backend is a drop-in
future upgrade behind the same signature.
"""
from __future__ import annotations

from .text import overlap_score, phrase_hit, tokenize


def _table_score(table, q_tokens, q_lower) -> int:
    score = 0
    score += 3 * overlap_score(q_tokens, table.name.replace("_", " "))
    score += 2 * overlap_score(q_tokens, table.description)
    for syn in table.synonyms:
        if phrase_hit(syn, q_lower):
            score += 3
    # column names/synonyms/descriptions are strong table signals
    for col in table.columns.values():
        score += overlap_score(q_tokens, col.name.replace("_", " "), col.description)
        for syn in col.synonyms:
            if phrase_hit(syn, q_lower):
                score += 2
    return score


def select_tables(layer, question, max_tables=8) -> list[str]:
    """Return up to ``max_tables`` real table names (uppercase) for the question:
    top-scored in-scope tables, then one certified-join hop to add partners."""
    candidates = layer.in_scope_tables()
    if not candidates:
        return []
    q_tokens = tokenize(question)
    q_lower = (question or "").lower()

    scored = sorted(
        candidates,
        key=lambda t: (_table_score(t, q_tokens, q_lower), t.row_count or 0),
        reverse=True)

    chosen, seen = [], set()

    def add(name):
        u = name.upper()
        if u not in seen and u in layer.tables and layer.tables[u].in_scope:
            chosen.append(layer.tables[u].name)
            seen.add(u)

    # If nothing scored, fall back to the biggest in-scope tables (join hubs).
    any_signal = _table_score(scored[0], q_tokens, q_lower) > 0
    for t in scored:
        if len(chosen) >= max_tables:
            break
        if any_signal and _table_score(t, q_tokens, q_lower) == 0:
            break
        add(t.name)

    if not chosen:  # no signal at all → biggest tables
        for t in scored[:max_tables]:
            add(t.name)

    # One certified-join hop so a chosen table's partners come along.
    for name in list(chosen):
        if len(chosen) >= max_tables:
            break
        for j in layer.joins_for(name):
            partner = j.right if j.left.upper() == name.upper() else j.left
            if len(chosen) < max_tables:
                add(partner)
    return chosen[:max_tables]
