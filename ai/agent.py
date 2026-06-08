"""The DIS analytics agent — a bounded reason → act → observe loop.

This turns the single-shot assistant into an *agentic* one: given a goal, the
model plans, calls the read-only tools in :mod:`ai.tools` (which wrap the
platform's real analytics/data/document capabilities), observes their results,
and either takes another step or writes a final grounded answer.

Why it is safe for a regulated lab:
  * **Bounded** — at most ``AGENT_MAX_STEPS`` tool-using turns, then a forced
    text answer; a hard cap on total tool calls as a backstop.
  * **Read-only & deterministic** — the model never computes figures itself; the
    tools do, and they only read. See :mod:`ai.tools`.
  * **Auditable** — every run returns a full ``trace`` of (tool, input, result),
    which the caller persists. Transparency is the feature.

The loop is provider-agnostic: it speaks the neutral message/tool vocabulary
defined on :class:`ai.providers.base.BaseProvider`, so it works identically on
Claude (cloud) and a local Qwen model (on-prem) with no code change.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from django.conf import settings

from .providers import AINotConfigured, get_provider
from .tools import execute_tool, tool_schemas

AGENT_SYSTEM_PROMPT = (
    "You are the DIS Analytics Agent — the agentic AI inside Decision "
    "Intelligence Suite (DIS), an AI-powered insights platform for modern "
    "laboratories (LIMS / quality-control data).\n\n"
    "You answer questions by INVESTIGATING the user's real data and documents "
    "with the tools provided — you never invent numbers. Method:\n"
    "1. FIND the data: call list_datasets — when many datasets exist, pass a "
    "`search` keyword from the question (e.g. search='spec', 'out-of-spec', "
    "'sulphur', 'product') to narrow it. Pick the dataset whose name/columns best "
    "match the question.\n"
    "2. INSPECT if unsure: describe_dataset reveals a dataset's exact columns and "
    "value ranges.\n"
    "3. ANALYSE — this is where you actually answer. Use the right tool(s): "
    "aggregate_data / compare_dimension (group & rank), run_statistical_test "
    "(ANOVA, t-test, regression, control charts, Cp/Cpk…), detect_anomalies / "
    "detect_anomalies_ml (outliers), root_cause (WHY an outcome happens — drivers "
    "of out-of-spec by product/analysis/operator), find_associations, cluster_data, "
    "forecast_metric, correlate_columns / discover_relationships, or query_dataset. "
    "Chain them when useful: e.g. compare_dimension to find the worst group, then "
    "root_cause to explain WHY.\n"
    "4. Use search_documents ONLY to ground answers in written specs/SOPs/ASTM-ISO "
    "standards (e.g. to quote a spec limit) — not for plain data questions.\n\n"
    "Rules:\n"
    "- Always respond in English, regardless of the language of the question.\n"
    "- list_datasets and describe_dataset are NAVIGATION, never a final answer. "
    "For a data question you MUST go on to run an ANALYSIS tool and answer with the "
    "actual findings — NEVER reply by merely listing or describing the available "
    "datasets (e.g. 'the dataset has 3000 rows and 8 columns' is NOT an answer). "
    "If you have only called discovery tools, you are not done.\n"
    "- Ground EVERY figure and claim in a tool result. If the tools genuinely "
    "cannot answer, say so and state what data is needed.\n"
    "- If NO available dataset fits the question, say exactly what data/dataset is "
    "missing — do NOT answer from an unrelated dataset (never use an off-spec "
    "dataset to answer a sample-type, total-count or octane question), and never "
    "invent company names or values.\n"
    "- Use exact dataset ids and column names from the tools; do not guess them.\n"
    "- Prefer the fewest ANALYSIS steps that answer the question; do not call "
    "search_documents for plain data questions. Stop only once an ANALYSIS tool "
    "(not a discovery tool) has produced the answer.\n"
    "- Synthesise from ALL the evidence you gathered, giving the MOST weight to "
    "the most decisive result — NOT just your most recent tool call. If an earlier "
    "tool (e.g. root_cause) directly answered the question, lead with that finding "
    "even if you ran other tools afterwards.\n"
    "- Final answer: be concise and specific for a lab operations manager. Lead "
    "with the takeaway, support it with the concrete figures you found, and note "
    "which dataset(s)/document(s) the evidence came from. Plain text, short "
    "paragraphs and bullets — avoid heavy markdown."
)


@dataclass
class AgentRun:
    """The outcome of one agent invocation."""

    answer: str
    trace: list = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    stopped_reason: str = "answered"  # answered | max_steps | empty | error

    def as_dict(self) -> dict:
        return {
            "answer": self.answer,
            "trace": self.trace,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "stopped_reason": self.stopped_reason,
        }


def _dataset_schema_text(dataset) -> str:
    """A compact column schema for the focused dataset so the model can go
    straight to analysis tools (no list/describe round-trips needed)."""
    from analytics.engine import build_dataframe, column_statistics

    df = build_dataframe(dataset)
    stats = column_statistics(df)
    cols = []
    for s in stats["statistics"]:
        if s["type"] == "numeric":
            cols.append(
                f"{s['column']} (numeric: min={s['min']}, max={s['max']}, mean={s['mean']})"
            )
        else:
            top = ", ".join(t["value"] for t in s.get("top_values", [])[:4])
            cols.append(f"{s['column']} (text: {s['distinct']} distinct; e.g. {top})")
    return (
        f"FOCUSED DATASET — id={dataset.id}, name=\"{dataset.name}\", "
        f"rows={stats['row_count']}.\nColumns (use these EXACT names):\n- "
        + "\n- ".join(cols)
        + "\n\nThis schema is already provided, so do NOT call list_datasets or "
        "describe_dataset for it — go straight to the analysis tools. Only list/"
        "inspect OTHER datasets if the question clearly needs them."
    )


def _dataset_catalog_text(user) -> str:
    """The user's REAL datasets (id, name, description, columns) injected into the
    system prompt so the model selects from actual datasets instead of inventing
    dataset names/ids — the dominant failure mode for small local models."""
    from .tools import _accessible_datasets

    rows = []
    for ds in _accessible_datasets(user).order_by("name")[:60]:
        cols = ", ".join(str(c) for c in (ds.cached_columns or [])[:12])
        desc = " ".join((ds.description or "").split())
        rows.append(f'- id={ds.id} "{ds.name}" ({ds.row_count} rows) — {desc} '
                    f'Columns: {cols}')
    if not rows:
        return ""
    return (
        "AVAILABLE DATASETS — these are the ONLY datasets that exist. Use these EXACT "
        "ids/names; never invent a dataset, id, column or value. You do NOT need to "
        "call list_datasets.\n" + "\n".join(rows) + "\n\nPick the dataset whose name/"
        "description best matches the question, call an analysis tool on its id, and "
        "answer with the real figures it returns. If none fits, say what data is missing."
    )


def _system_blocks(dataset=None, user=None):
    blocks = [{"type": "text", "text": AGENT_SYSTEM_PROMPT}]
    if dataset is not None:
        try:
            text = _dataset_schema_text(dataset)
        except Exception:  # noqa: BLE001 - schema is best-effort
            text = (
                f"The user is focused on dataset id={dataset.id} "
                f"(\"{dataset.name}\"). Prefer it unless the question points elsewhere."
            )
        blocks.append({"type": "text", "text": text})
    elif user is not None:
        # No focused dataset: inject the real catalog so the model selects from
        # actual datasets/ids rather than hallucinating them.
        try:
            catalog = _dataset_catalog_text(user)
        except Exception:  # noqa: BLE001 - best-effort
            catalog = ""
        if catalog:
            blocks.append({"type": "text", "text": catalog})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _json_safe(obj) -> str:
    """Serialise a tool result for the model, tolerating odd types."""
    try:
        return json.dumps(obj, default=str)
    except (TypeError, ValueError):
        return json.dumps({"result": str(obj)})


def _extract_text_tool_calls(text, tool_names):
    """Recover tool calls a model wrote as TEXT instead of native function calls.

    Local/quantized models (via Ollama) often emit, e.g.,
    ``{"name": "list_datasets", "arguments": {...}}`` (optionally in a ```json
    fence or <tool_call> tags) in the message body. Without this, the loop would
    treat that preamble as the final answer and quit after one step. Returns
    native-shape calls ([{name, input, id}]); only matches known tool names.
    """
    if not text or "{" not in text:
        return []
    calls = []
    for chunk in re.findall(r"\{(?:[^{}]|\{[^{}]*\})*\}", text):
        try:
            obj = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
        if name not in tool_names:
            continue
        args = obj.get("input")
        if args is None:
            args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {}
        calls.append({
            "name": name,
            "input": args if isinstance(args, dict) else {},
            "id": f"text_call_{len(calls)}",
        })
    return calls


def run_agent(user, history, dataset=None, max_steps=None, max_tokens=2048) -> AgentRun:
    """Run the agent to answer the latest message in ``history``.

    ``history`` is ``[{"role": "user"|"assistant", "content": str}, ...]`` ending
    in the user's question. ``dataset`` (optional) is the focused dataset.
    Returns an :class:`AgentRun` (answer + evidence trace).
    """
    provider = get_provider()
    if not provider.configured:
        raise AINotConfigured(provider.not_configured_message())

    if max_steps is None:
        max_steps = int(getattr(settings, "AGENT_MAX_STEPS", 6))
    max_tool_calls = int(getattr(settings, "AGENT_MAX_TOOL_CALLS", 24))

    system_blocks = _system_blocks(dataset, user)
    schemas = tool_schemas()
    tool_names = {s["name"] for s in schemas}
    messages = [dict(m) for m in (history or [])]

    trace: list = []
    total_tool_calls = 0
    last_text = ""
    empty_turns = 0  # guard against small models returning a blank turn

    for step in range(max_steps):
        # Once we hit the tool-call backstop, stop offering tools so the model wraps up.
        offer_tools = schemas if total_tool_calls < max_tool_calls else []
        turn = provider.complete(
            system_blocks=system_blocks,
            messages=messages,
            tools=offer_tools,
            max_tokens=max_tokens,
        )
        text = (turn.get("text") or "").strip()
        calls = turn.get("tool_calls") or []

        # Local models often emit a tool call as TEXT rather than a native function
        # call — recover it so the loop continues instead of quitting with a useless
        # "let me call list_datasets…" preamble (the #1 cause of 1-step give-ups).
        if not calls and text:
            recovered = _extract_text_tool_calls(text, tool_names)
            if recovered:
                calls, text = recovered, ""

        if text:
            last_text = text

        if not calls:
            if text:
                return AgentRun(
                    answer=text, trace=trace, steps=step + 1,
                    tool_calls=total_tool_calls, stopped_reason="answered",
                )
            # Blank turn (no text, no tools) — nudge the model to answer; bail if
            # it keeps stalling rather than looping forever.
            empty_turns += 1
            if empty_turns > 2:
                return AgentRun(
                    answer=last_text or "I could not produce an answer from the "
                    "available data. Please rephrase or point me at a dataset.",
                    trace=trace, steps=step + 1, tool_calls=total_tool_calls,
                    stopped_reason="empty",
                )
            messages.append({
                "role": "user",
                "content": "Please give your final answer now, based on the tool "
                "results above. If you need another tool, call it.",
            })
            continue

        # Record the assistant's tool-calling turn, then run each call.
        messages.append({
            "role": "assistant",
            "content": turn.get("text") or "",
            "tool_calls": calls,
        })
        for call in calls:
            result = execute_tool(call["name"], user, call.get("input") or {})
            total_tool_calls += 1
            trace.append({
                "step": step + 1,
                "tool": call["name"],
                "input": call.get("input") or {},
                "result": result,
                "ok": not (isinstance(result, dict) and "error" in result),
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": _json_safe(result),
            })

    # Ran out of steps while still wanting tools — force a final text answer.
    final = provider.complete(
        system_blocks=system_blocks,
        messages=messages + [{
            "role": "user",
            "content": (
                "You have reached the analysis-step limit. Give your best final "
                "answer now using everything you have gathered. Do not call more tools."
            ),
        }],
        tools=[],
        max_tokens=max_tokens,
    )
    answer = final.get("text") or last_text or "(no answer)"
    return AgentRun(
        answer=answer, trace=trace, steps=max_steps, tool_calls=total_tool_calls,
        stopped_reason="max_steps",
    )
