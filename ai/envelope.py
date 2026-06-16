"""The AnswerEnvelope — the single typed object that flows from Talk to Visualize
to Report (DIS_TalkToData_Plan.md §6).

One question produces one envelope: the narrative, the result table, a
DETERMINISTICALLY-chosen chart spec (no LLM — reproducible and audit-defensible),
and full provenance (the exact SQL, the path taken, the model, freshness, who/when).
Ask Data renders it, a dashboard widget pins it, and an export packages it — all
reading the same object.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from django.utils import timezone

_TIME_NAME_RE = re.compile(r"date|time|month|year|day|week|quarter|period|_at\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?")


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            return math.isfinite(float(v.replace(",", "")))  # rejects "nan"/"inf"
        except ValueError:
            return False
    return False


def _looks_temporal(name, sample_values) -> bool:
    if _TIME_NAME_RE.search(str(name or "")):
        return True
    for v in sample_values[:5]:
        if isinstance(v, str) and _ISO_DATE_RE.match(v.strip()):
            return True
    return False


def suggest_chart(columns, rows) -> dict | None:
    """Deterministic chart choice; None means 'render as a table'.

    1 numeric scalar → kpi; a (label, number) pair → line if the label is temporal,
    pie if few categories, else bar; anything wider/non-numeric → table.
    """
    if not columns or not rows:
        return None
    ncols = len(columns)

    if ncols == 1 and len(rows) == 1 and _is_number(rows[0][0]):
        return {"type": "kpi", "value_field": columns[0]}

    if ncols == 2:
        cat, val = columns[0], columns[1]
        values = [r[1] for r in rows if len(r) > 1 and r[1] is not None]
        if values and all(_is_number(v) for v in values):
            if _looks_temporal(cat, [r[0] for r in rows]):
                return {"type": "line", "category_field": cat, "value_field": val}
            if len(rows) <= 6:
                return {"type": "pie", "category_field": cat, "value_field": val}
            return {"type": "bar", "category_field": cat, "value_field": val}
    return None  # 3+ columns, or non-numeric measure → table


@dataclass
class AnswerEnvelope:
    schema_version: int = 1
    understood: bool = True
    narrative: str = ""
    result: dict = field(default_factory=lambda: {
        "columns": [], "rows": [], "row_count": 0, "truncated": False})
    chart: dict | None = None
    provenance: dict = field(default_factory=dict)
    error: str | None = None
    suggestions: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return timezone.now().isoformat()


def envelope_from_result(result, *, narrative="", understood=None, error=None,
                         provenance=None) -> AnswerEnvelope:
    """Build an envelope from an ask_database / runner result dict."""
    result = result or {}
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    res = {
        "columns": columns,
        "rows": rows,
        "row_count": result.get("row_count", len(rows)),
        "truncated": bool(result.get("truncated", False)),
    }
    prov = {
        "sql": result.get("sql"),
        "path": result.get("path"),
        "freshness": result.get("freshness"),
        "datasource": result.get("datasource"),
        "generated_at": _now_iso(),
    }
    prov.update(provenance or {})
    understood = result.get("understood", True) if understood is None else understood
    return AnswerEnvelope(
        understood=bool(understood),
        narrative=narrative,
        result=res,
        chart=suggest_chart(columns, rows),
        provenance={k: v for k, v in prov.items() if v is not None},
        error=error or result.get("error"),
        suggestions=result.get("available_tables", []) if not understood else [],
    )


def _tabular_from_trace(trace):
    """Pull the most relevant table the agent produced for charting/provenance —
    prefer an ask_database result (it carries SQL), else the last rows-bearing tool."""
    best = None
    for entry in trace or []:
        res = entry.get("result")
        if not isinstance(res, dict):
            continue
        cols, rows = res.get("columns"), res.get("rows")
        if isinstance(cols, list) and isinstance(rows, list):
            if entry.get("tool") == "ask_database":
                best = res  # strongest signal; keep the latest
            elif best is None:
                best = res
    return best


def _last_ask_database(trace):
    last = None
    for entry in trace or []:
        if entry.get("tool") == "ask_database" and isinstance(entry.get("result"), dict):
            last = entry["result"]
    return last


def envelope_from_agent_run(run, *, model="", user_id=None) -> AnswerEnvelope:
    """Package a full agent run (narrative + evidence) into an envelope."""
    trace = getattr(run, "trace", [])
    tabular = _tabular_from_trace(trace)

    # If the only DB evidence was a *failed* ask_database, reflect that honestly
    # rather than claiming success with an empty table.
    understood = True
    fail = None
    if tabular is None:
        last_db = _last_ask_database(trace)
        if last_db is not None and last_db.get("understood") is False:
            understood, fail = False, last_db

    env = envelope_from_result(
        tabular or {},
        narrative=getattr(run, "answer", "") or "",
        understood=understood,
        error=(fail or {}).get("error"),
        provenance={
            "model": model,
            "agent_steps": getattr(run, "steps", None),
            "tool_calls": getattr(run, "tool_calls", None),
            "stopped_reason": getattr(run, "stopped_reason", None),
            "user_id": user_id,
            "path": (tabular or {}).get("path") or "agent",
        },
    )
    if fail and fail.get("available_tables"):
        env.suggestions = fail["available_tables"]
    return env
