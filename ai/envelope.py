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

from . import confidence

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
    # How much to trust this — a LEVEL with reasons, never a percentage. Built entirely from
    # facts the runner already had and this envelope used to throw away: how many times the
    # model's SQL failed validation, the post-execution sanity warnings, how stale the data
    # is. See ai/confidence.py.
    confidence: dict = field(default_factory=dict)
    error: str | None = None
    suggestions: list = field(default_factory=list)
    # When we cannot answer: the certified metrics we CAN. A refusal that stops at "I don't
    # know" is a dead end; one that says "not that — but I can tell you these, exactly, from
    # approved SQL" is the product's argument made when it is most persuasive.
    can_answer: list = field(default_factory=list)

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
        # How old the underlying data is. Reserved in the export footer since day
        # one and never populated — an answer off a 3-day-stale replica used to
        # look identical to a live one. replication.freshness now fills it.
        "data_as_of": result.get("data_as_of"),
        "freshness_detail": result.get("freshness_detail"),
        "datasource": result.get("datasource"),
        "generated_at": _now_iso(),
    }
    prov.update(provenance or {})
    understood = result.get("understood", True) if understood is None else understood

    # The runner counted `attempts` and collected `warnings` on every single request, and
    # they died right here. An answer the model got right first time and one it got wrong
    # twice looked identical on screen.
    graded = confidence.grade({**result, "understood": understood})
    prov["attempts"] = _attempts_int(result)
    if result.get("warnings"):
        prov["warnings"] = list(result["warnings"])

    return AnswerEnvelope(
        understood=bool(understood),
        narrative=narrative,
        result=res,
        chart=suggest_chart(columns, rows),
        provenance={k: v for k, v in prov.items() if v is not None},
        confidence=graded,
        error=error or result.get("error"),
        suggestions=result.get("available_tables", []) if not understood else [],
        can_answer=(confidence.what_i_can_answer(_datasource_of(result))
                    if not understood else []),
    )


def _attempts_int(result) -> int:
    a = result.get("attempts")
    if isinstance(a, int):
        return a
    if isinstance(a, list):
        return len(a) + 1
    return 1


def _datasource_of(result):
    """The DataSource the question was asked against, if we can still find it."""
    ds = (result or {}).get("datasource") or {}
    ds_id = ds.get("id") if isinstance(ds, dict) else None
    if not ds_id:
        return None
    from connections.models import DataSource

    return DataSource.objects.filter(pk=ds_id).first()


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
