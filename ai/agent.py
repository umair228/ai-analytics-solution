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
    "Use ask_database to query the LIVE database directly — it writes and runs a "
    "safe read-only SQL query over the real schema and returns the rows + the exact "
    "SQL. For a SPECIFIC factual question about the records (a count, a rate, a "
    "ranking/top-N, a breakdown by product/test/operator), PREFER ask_database — do "
    "NOT answer by listing datasets, and do NOT call query_dataset with a guessed id. "
    "Use the materialized datasets only for the pre-built summary dashboards. "
    "Chain them when useful: e.g. compare_dimension to find the worst group, then "
    "root_cause to explain WHY.\n"
    "4. Use search_documents ONLY to ground answers in written specs/SOPs/ASTM-ISO "
    "standards (e.g. to quote a spec limit) — not for plain data questions.\n\n"
    "Rules:\n"
    "- Always respond in English, regardless of the language of the question.\n"
    "- If the user is only greeting you, thanking you, asking who you are / what you "
    "can do, or making small talk (no actual data question), reply briefly and warmly "
    "in plain English — introduce yourself and suggest 2-3 example questions — and do "
    "NOT call any tool or re-answer a previous question.\n"
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
    "- For anything the materialized datasets don't cover, use ask_database (live SQL "
    "over the real schema, any table/column) BEFORE concluding data is missing. Trust "
    "only the rows it returns and report the figure; never invent columns or values. "
    "When you call ask_database, pass the datasource_id from the AVAILABLE DATABASES "
    "list (or omit it to use the default) — never invent a dataset id or datasource id, "
    "and do NOT ask the user which database to use.\n"
    "- PRE-AGGREGATED datasets (names like '… by Product', '… by Test', 'Sample "
    "Types', 'Status Breakdown', 'Overview Totals', 'Key Parameter Ranges') are "
    "ALREADY summarised and sorted — the count/value is IN A COLUMN (e.g. samples, "
    "off_spec, value, min_value/max_value). Answer them with query_dataset and READ "
    "the relevant row/column (e.g. the top row, or the row whose analysis matches). "
    "Do NOT run compare_dimension or aggregate_data on them — that re-counts the "
    "rows and returns wrong numbers like 1. Use compare_dimension/aggregate_data "
    "ONLY on row-level datasets ('Sample Register', 'Results (row-level)', 'Off-Spec "
    "Results', 'Sulphur Results').\n"
    "- Prefer the fewest ANALYSIS steps that answer the question; do not call "
    "search_documents for plain data questions. Stop only once an ANALYSIS tool "
    "(not a discovery tool) has produced the answer.\n"
    "- Synthesise from ALL the evidence you gathered, giving the MOST weight to "
    "the most decisive result — NOT just your most recent tool call. If an earlier "
    "tool (e.g. root_cause) directly answered the question, lead with that finding "
    "even if you ran other tools afterwards.\n"
    "- Your FINAL message MUST state the answer with the actual numbers from the "
    "tool results (e.g. 'SCHEDULED — 53,344 samples'). NEVER end by restating the "
    "plan ('To find…', 'I will use dataset id=…') or describing a dataset — that is "
    "a failure. If you named a dataset, you must then read it and give the figure.\n"
    "- Final answer: be concise and specific for a lab operations manager. Lead "
    "with the takeaway + the concrete figures you found, and note which dataset(s)/"
    "document(s) the evidence came from. Plain text, short paragraphs/bullets — "
    "avoid heavy markdown."
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


def _datasource_catalog_text(user) -> str:
    """The user's live DATABASE connections (id, name, type) injected so the model
    knows it can reach the whole database via ask_database — and which id to pass."""
    from .tools import _accessible_datasources

    rows = []
    for ds in _accessible_datasources(user)[:20]:
        db = ds.database_name or ""
        desc = " ".join((ds.description or "").split())
        rows.append(
            f'- id={ds.id} "{ds.name}" [{ds.source_type}{("/" + db) if db else ""}]'
            + (f" — {desc}" if desc else "")
        )
    if not rows:
        return ""
    return (
        "AVAILABLE DATABASES — live connections you can query directly with the "
        "ask_database tool (it writes & runs a safe read-only SQL query over the real "
        "schema and returns the rows + the exact SQL). Pass the id as datasource_id. "
        "Use this for any question the materialized datasets above do not cover.\n"
        + "\n".join(rows)
    )


# SQL-FIRST mode (enabled when DSE_TEXTTOSQL_DEFAULT_DATASOURCE is set): the agent
# answers from the LIVE database via ask_database instead of the pre-built datasets.
# Eval showed the model otherwise wanders into query_dataset/compare_dimension on the
# materialized summaries and MISREADS them (row counts, wrong by-count-vs-rate, missing
# labels). This forces the reliable path and flags the known company/customer data gap.
SQL_FIRST_DIRECTIVE = (
    "ANSWER MODE — LIVE DATABASE.\n"
    "Answer EVERY factual question (counts, totals, rates, rankings/top-N, breakdowns "
    "by product/test/operator/date) by calling ask_database with the user's question — "
    "it writes and runs read-only SQL over the real tables and returns exact rows with "
    "the SQL. ask_database is your PRIMARY tool; call it FIRST.\n"
    "Do NOT use list_datasets, query_dataset, compare_dimension, aggregate_data or "
    "describe_dataset for these questions — they read tiny pre-built summaries and give "
    "WRONG numbers (e.g. a row count instead of the value, or by-count when asked "
    "by-rate). Use search_documents only for standards/method questions (ASTM/ISO), and "
    "the forecast tools only for explicit 'forecast / next month' questions.\n"
    "DATA GAP: this database has NO populated company/customer dimension "
    "(SAMPLE.CUSTOMER is ~93% empty). For any 'which company / customer / interlab' "
    "question, state that company/customer data is not available — do NOT answer it "
    "with products or operators."
)


def _system_blocks(dataset=None, user=None):
    sql_first = bool(getattr(settings, "DSE_TEXTTOSQL_DEFAULT_DATASOURCE", ""))
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
    elif user is not None and not sql_first:
        # No focused dataset: inject the real catalog so the model selects from
        # actual datasets/ids rather than hallucinating them. Skipped in SQL-first
        # mode so the 19 summary datasets don't distract the agent from ask_database.
        try:
            catalog = _dataset_catalog_text(user)
        except Exception:  # noqa: BLE001 - best-effort
            catalog = ""
        if catalog:
            blocks.append({"type": "text", "text": catalog})
    if user is not None:
        try:
            ds_catalog = _datasource_catalog_text(user)
        except Exception:  # noqa: BLE001 - best-effort
            ds_catalog = ""
        if ds_catalog:
            blocks.append({"type": "text", "text": ds_catalog})
    if sql_first and dataset is None:
        blocks.append({"type": "text", "text": SQL_FIRST_DIRECTIVE})
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


# Greetings / capability / chit-chat — handled warmly WITHOUT running a query, so
# "hi" or "what can you do" gets a proactive intro instead of being forced into SQL
# (or worse, re-answering the previous question because of conversation history).
_SMALLTALK_RE = re.compile(
    r"^\s*("
    r"hi+|hey+|hello+|hiya|yo|sup|greetings|good\s+(morning|afternoon|evening|day)|"
    r"thanks|thank\s+you|thx|ty|cheers|ok|okay|kk|cool|great|nice|awesome|got\s+it|"
    r"who\s+are\s+you|what\s+(can|do)\s+you\s+(do|help\s+with)|what\s+do\s+you\s+do|"
    r"what\s+is\s+this|what\s+can\s+i\s+ask|how\s+do\s+you\s+work|how\s+can\s+you\s+help|"
    r"help|hello\s+there|test|ping|are\s+you\s+there|what\s+are\s+your\s+capabilities"
    r")\s*[!.?…]*\s*$",
    re.IGNORECASE,
)

_ASSISTANT_INTRO = (
    "Hi! I'm the DIS analytics assistant for your EGPC laboratory data. Ask me anything "
    "about your samples, results, products, tests, off-spec rates, operators or "
    "turnaround — in plain English. I can also run statistics, detect anomalies, find "
    "root causes, forecast trends, and look up lab standards (ASTM/ISO).\n\n"
    "Here are a few things you can try:\n\n"
    "- Which product has the highest out-of-spec rate?\n"
    "- How many results are out of specification?\n"
    "- What are the top 5 most-used tests?\n"
    "- Average sulphur by product, highest first\n"
    "- Forecast next month's sample volume\n\n"
    "What would you like to know?"
)


def _smalltalk_fast_path(history):
    """Return a friendly intro AgentRun for greetings / capability / chit-chat
    messages (no tools, no LLM), else None to fall through to the normal flow."""
    msg = next((m.get("content", "").strip() for m in reversed(history or [])
                if m.get("role") == "user" and m.get("content")), "")
    if not msg or len(msg.split()) > 6 or not _SMALLTALK_RE.match(msg):
        return None
    return AgentRun(answer=_ASSISTANT_INTRO, trace=[], steps=0, tool_calls=0,
                    stopped_reason="greeting")


def _format_metric_answer(result) -> str:
    """Render a certified-metric result's rows into a short, figure-bearing answer
    (the deterministic fallback when no LLM is available to narrate)."""
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if not rows:
        return "No matching rows were found for that question."
    top = ", ".join(f"{c}={v}" for c, v in zip(cols, rows[0]))
    if len(rows) == 1:
        return top
    return f"{top}  (top of {result.get('row_count', len(rows))} rows)"


def _narrate_metric(question, result) -> str:
    """Turn the exact metric rows into a brief, human English answer via ONE grounded
    LLM call — the figures are handed to the model (it must not change them), so the
    answer is deterministic in its numbers but natural in its wording. Falls back to a
    structured line if no LLM is configured."""
    structured = _format_metric_answer(result)
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if not rows:
        return structured
    try:
        provider = get_provider()
        if not provider.configured:
            return structured
        table = "\n".join(
            ", ".join(f"{c}={v}" for c, v in zip(cols, r)) for r in rows[:20])
        system = (
            "You are a laboratory data analyst. Write a brief, clear answer to the "
            "user's question for a lab operations manager, using ONLY the result data "
            "provided — never invent, add, or change a number. Lead with the key "
            "figure(s); 1-3 short sentences, plain English, no markdown tables.")
        user = (f"Question: {question}\n\nExact result data (already computed — do not "
                f"alter any value):\n{table}")
        text = provider.chat(
            system_blocks=[{"type": "text", "text": system}],
            messages=[{"role": "user", "content": user}], max_tokens=220)
        return (text or "").strip() or structured
    except Exception:  # noqa: BLE001 - narration is best-effort; never break the answer
        return structured


def _certified_fast_path(user, history, dataset):
    """Answer deterministically from a curated metric when the VERBATIM user question
    maps to one — so the agent can't rephrase past the metric (SQL-first mode only).
    Returns an AgentRun, or None to fall through to the normal agent loop. Needs no LLM."""
    if dataset is not None or not getattr(settings, "DSE_TEXTTOSQL_DEFAULT_DATASOURCE", ""):
        return None
    question = next((m.get("content", "").strip() for m in reversed(history or [])
                     if m.get("role") == "user" and m.get("content")), "")
    if not question:
        return None
    try:
        from ai.tools import _resolve_datasource
        from semantics.layer import get_semantic_layer
        from semantics.router import match_certified_metric
        from texttosql.runner import run_text_to_sql

        ds = _resolve_datasource(user, None)
        layer = get_semantic_layer(ds)
        if not layer.is_present() or match_certified_metric(layer, question) is None:
            return None
        result = run_text_to_sql(ds, question)
    except Exception:  # noqa: BLE001 - never block the agent on the fast path
        return None
    if not (isinstance(result, dict) and result.get("understood")
            and str(result.get("path", "")).startswith("certified_metric")):
        return None
    return AgentRun(
        answer=_narrate_metric(question, result),
        trace=[{"step": 1, "tool": "ask_database", "input": {"question": question},
                "result": result, "ok": True}],
        steps=1, tool_calls=1, stopped_reason="certified_metric")


def run_agent(user, history, dataset=None, max_steps=None, max_tokens=2048) -> AgentRun:
    """Run the agent to answer the latest message in ``history``.

    ``history`` is ``[{"role": "user"|"assistant", "content": str}, ...]`` ending
    in the user's question. ``dataset`` (optional) is the focused dataset.
    Returns an :class:`AgentRun` (answer + evidence trace).
    """
    greeting = _smalltalk_fast_path(history)
    if greeting is not None:
        return greeting

    fast = _certified_fast_path(user, history, dataset)
    if fast is not None:
        return fast

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
            # A plan-preamble after tools already ran ("To find…", "I will use
            # dataset id=…") is NOT an answer — nudge for the actual figures.
            plan_like = bool(text) and bool(re.match(
                r"(to find|to answer|to determine|to calculate|i will|i'?ll|"
                r"let me|first[,: ]|the dataset that)", text.lower()))
            if text and not (plan_like and trace):
                return AgentRun(
                    answer=text, trace=trace, steps=step + 1,
                    tool_calls=total_tool_calls, stopped_reason="answered",
                )
            # Blank turn, or a plan-preamble after running tools — nudge; bail if it
            # keeps stalling rather than looping forever.
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
                "content": "Give your FINAL answer now with the ACTUAL NUMBERS from "
                "the tool results above — do not restate the plan or name a dataset "
                "without giving its figure. If you genuinely need another tool, call it.",
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
