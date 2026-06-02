"""
Natural-language -> SQL for AI Document Search.

Ported from the LW-Chatbot ``nlp_to_sql.py`` and **corrected to the real
SMJMUN_DEV LabWare schema** (verified live):

  * ``v_lw_nlp_bot`` exists but has only 5 columns
    (RESPONSE, NAME, DATE_CREATED, CREATED_BY, TEMPLATE) — the original's
    ``c_payment_link`` / ``c_po_number`` columns do NOT exist here.
  * SAMPLE / TEST / RESULT exist with hundreds of columns, but the original's
    ``collected_date`` / ``test_date`` / ``result_value`` do NOT — the field maps
    below use columns that actually exist. SQL Server is case-insensitive, so the
    lowercase field keys resolve to the real UPPERCASE columns.

Robustness (post-review):
  * NL->SQL only fires on a confident data-intent signal (a condition, a "strong"
    field, or >=2 distinct fields) so generic documentation questions don't run a
    query (``run_nl_sql`` returns ``None`` -> caller falls back to document search).
  * Unrelated tables are pruned to the join-reachable set — never a CROSS JOIN.
  * Rows are serialised JSON-safe (NaT/NaN -> null, datetimes -> ISO strings).
  * Date/number tokens are parsed defensively (no uncaught ValueError/OverflowError).
  * created_by / template LIKE values escape ``%`` and ``_`` wildcards.

The query runs over ``docsearch.db`` (the dedicated MSSQL connection).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .db import get_connection, is_sqlite, read_sql, tbl

# ───── Metadata (logical name -> real table + fields), schema-verified ─────
metadata: dict[str, dict] = {
    "sample": {
        "table": "SAMPLE",
        "fields": {
            "sample_number": "string", "text_id": "string", "status": "string",
            "product": "string", "sample_type": "string", "project": "string",
            "login_date": "date", "date_completed": "date", "login_by": "string",
        },
        "joinFields": {"test": ["sample_number"], "result": ["sample_number"]},
    },
    "test": {
        "table": "TEST",
        "fields": {
            "test_number": "string", "sample_number": "string", "analysis": "string",
            "status": "string", "lab": "string", "instrument": "string",
            "date_completed": "date", "assigned_operator": "string",
        },
        "joinFields": {"sample": ["sample_number"], "result": ["test_number", "sample_number"]},
    },
    "result": {
        "table": "RESULT",
        "fields": {
            "sample_number": "string", "test_number": "string", "name": "string",
            "formatted_entry": "string", "units": "string", "status": "string",
            "entered_on": "date", "entered_by": "string",
        },
        "joinFields": {"sample": ["sample_number"], "test": ["sample_number", "test_number"]},
    },
    "v_lw_nlp_bot": {
        "table": "v_lw_nlp_bot",
        "fields": {
            "response": "string", "name": "string", "date_created": "date",
            "created_by": "string", "template": "string",
        },
        "joinFields": {},  # standalone view — never CROSS JOIN-ed (see _prune_unreachable)
    },
}

# Ultra-common English words that double as column names. A bare match on one of
# these alone is NOT enough to treat a question as a structured-data query.
COMMON_FIELDS = {"name", "status", "product", "project", "units", "analysis"}


def _lit(v: str) -> str:
    """Escape a string literal for inline SQL (doubles single quotes)."""
    return str(v).replace("'", "''")


def _like_lit(v: str) -> str:
    """Escape LIKE wildcards (% and _) and the escape char, then quotes.
    Pairs with an ``ESCAPE '\\'`` clause (portable across MSSQL and SQLite)."""
    s = str(v).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return s.replace("'", "''")


def _format_date(d) -> str:
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    return f"{d:%Y-%m-%d}"


def _safe_date(s: str):
    """Parse DD/MM/YYYY, returning None instead of raising on bad input."""
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None


def _parse_explicit_range(prompt: str):
    m = re.search(r"(\b\w+\s*date\b)\s+from\s+(\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})", prompt)
    if not m:
        return None
    field, start, end = m.groups()
    sd, ed = _safe_date(start), _safe_date(end)
    if not sd or not ed:
        return None
    return {"field": field, "startDate": str(sd), "endDate": str(ed)}


def _parse_relative_days(prompt: str):
    m = re.search(r"for\s+(\d{1,6})\s+days", prompt)  # bounded digits -> no OverflowError
    if not m:
        return None
    days = min(int(m.group(1)), 36500)  # clamp to ~100 years
    return _format_date(datetime.today() - timedelta(days=days))


def _field_mentioned(field: str, prompt: str) -> bool:
    nf = field.replace("_", " ")
    return bool(re.search(rf"\b{re.escape(nf)}\b", prompt))


def _require(pq: dict, t: str):
    if t not in pq["requiredTables"]:
        pq["requiredTables"].append(t)


def _prune_unreachable(pq: dict):
    """Drop required tables (and their fields/conditions) that cannot be INNER-joined
    to the primary table, so generate_sql never emits a CROSS JOIN cartesian product."""
    req = pq["requiredTables"]
    if len(req) <= 1:
        return
    main = req[0]
    adj = {t: set() for t in req}
    for s in req:
        for tgt in metadata[s]["joinFields"]:
            if tgt in adj:
                adj[s].add(tgt)
                adj[tgt].add(s)
    reach, stack = {main}, [main]
    while stack:
        for nbr in adj[stack.pop()]:
            if nbr not in reach:
                reach.add(nbr)
                stack.append(nbr)
    pq["requiredTables"] = [t for t in req if t in reach]
    pq["fields"] = [f for f in pq["fields"] if f["table"] in reach]
    pq["conditions"] = [c for c in pq["conditions"] if c["table"] in reach]
    pq["joins"] = [j for j in pq["joins"]
                   if j["sourceTable"] in reach and j["targetTable"] in reach]


def parse_prompt(prompt: str) -> dict:
    """Parse a prompt into a minimal query DSL. Empty ``fields`` => not a data query."""
    pq = {"action": "SELECT", "fields": [], "joins": [], "conditions": [], "requiredTables": []}
    p = prompt.lower()

    # Fast-path: status / request queries pull the whole nlp_bot view.
    if any(k in p for k in ("project status", "request status", "sample status", "samples status")):
        pq["fields"] = [{"table": "v_lw_nlp_bot", "field": f} for f in metadata["v_lw_nlp_bot"]["fields"]]
        pq["requiredTables"].append("v_lw_nlp_bot")
        return pq

    # Field discovery across all known tables.
    for tbl_key, info in metadata.items():
        for fld in info["fields"]:
            if _field_mentioned(fld, p):
                pq["fields"].append({"table": tbl_key, "field": fld})
                _require(pq, tbl_key)

    # created by <user>
    m = re.search(r"created by ([\w.]+)", p)
    if m:
        pq["conditions"].append({"table": "v_lw_nlp_bot", "field": "created_by", "value": m.group(1).upper()})
        _require(pq, "v_lw_nlp_bot")

    # on <DD/MM/YYYY>  -> half-open single-day range (datetime-safe)
    m = re.search(r"on\s+(\d{2}/\d{2}/\d{4})", p)
    if m:
        d = _safe_date(m.group(1))
        if d:
            pq["conditions"].append({"table": "v_lw_nlp_bot", "field": "date_created",
                                     "startDate": str(d), "endExclusive": str(d + timedelta(days=1))})
            _require(pq, "v_lw_nlp_bot")

    # template <name>
    m = re.search(r"template\s+(\w+)", p)
    if m:
        pq["conditions"].append({"table": "v_lw_nlp_bot", "field": "template", "value": m.group(1).upper()})
        _require(pq, "v_lw_nlp_bot")

    # explicit date range
    dr = _parse_explicit_range(p)
    if dr:
        for tbl_key, info in metadata.items():
            for fld, tp in info["fields"].items():
                if tp == "date" and fld.replace("_", " ") == dr["field"]:
                    pq["conditions"].append({"table": tbl_key, "field": fld,
                                             "startDate": dr["startDate"], "endDate": dr["endDate"]})
                    _require(pq, tbl_key)

    # relative "for N days"
    since = _parse_relative_days(p)
    if since:
        for tbl_key, info in metadata.items():
            for fld, tp in info["fields"].items():
                if tp == "date":
                    pq["conditions"].append({"table": tbl_key, "field": fld, "startDate": since})
                    _require(pq, tbl_key)
                    break
            if tbl_key in pq["requiredTables"]:
                break

    # auto-join discovery
    for src in list(pq["requiredTables"]):
        for tgt, keys in metadata[src]["joinFields"].items():
            if tgt in pq["requiredTables"]:
                for k in (keys if isinstance(keys, list) else [keys]):
                    pq["joins"].append({"sourceTable": src, "sourceField": k, "targetTable": tgt, "targetField": k})

    # Never CROSS JOIN unrelated tables — keep only the join-reachable set.
    _prune_unreachable(pq)

    # Data-intent gate: a generic documentation question (e.g. only the bare word
    # "name" matched) must NOT run SQL. Require a condition, a strong field, or >=2
    # distinct field names.
    distinct_fields = {f["field"] for f in pq["fields"]}
    strong = any(fn not in COMMON_FIELDS for fn in distinct_fields)
    confident = bool(pq["conditions"]) or strong or len(distinct_fields) >= 2
    if not confident:
        pq["fields"] = []
        return pq

    # Back-fill: if we have a confident data query (e.g. a date condition) but no
    # explicit field tokens, select the required tables' full field set.
    if not pq["fields"]:
        for t in pq["requiredTables"]:
            pq["fields"] += [{"table": t, "field": fld} for fld in metadata[t]["fields"]]

    # Ensure the view returns its full set of columns when used.
    if "v_lw_nlp_bot" in pq["requiredTables"]:
        present = {f["field"] for f in pq["fields"]}
        for fld in metadata["v_lw_nlp_bot"]["fields"]:
            if fld not in present:
                pq["fields"].append({"table": "v_lw_nlp_bot", "field": fld})

    return pq


def generate_sql(pq: dict, limit: int = 100):
    """Build SQL from the parsed DSL, execute it, return (DataFrame, sql_string)."""
    if not pq["fields"]:
        raise ValueError("No matching fields found in the prompt.")

    alias = {t: t[0].upper() for t in pq["requiredTables"]}
    top = "" if is_sqlite() else f"TOP {int(limit)} "
    select_list = ", ".join(
        f"{alias[f['table']]}.{f['field']} AS {f['table']}__{f['field']}" for f in pq["fields"]
    )

    main = pq["requiredTables"][0]
    sql = f"{pq['action']} {top}{select_list} FROM {tbl(metadata[main]['table'])} AS {alias[main]}"

    joined = {main}
    for j in pq["joins"]:
        tgt = j["targetTable"]
        if tgt not in joined:
            sql += (f" INNER JOIN {tbl(metadata[tgt]['table'])} AS {alias[tgt]} ON "
                    f"{alias[j['sourceTable']]}.{j['sourceField']} = {alias[tgt]}.{j['targetField']}")
            joined.add(tgt)

    unjoined = [t for t in pq["requiredTables"] if t not in joined]
    if unjoined:  # pruning should have prevented this; never CROSS JOIN
        raise ValueError(f"Unable to join tables: {unjoined}")

    if pq["conditions"]:
        parts = []
        for c in pq["conditions"]:
            a, f = alias[c["table"]], c["field"]
            if "startDate" in c and "endExclusive" in c:
                parts.append(f"{a}.{f} >= '{_lit(c['startDate'])}' AND {a}.{f} < '{_lit(c['endExclusive'])}'")
            elif "startDate" in c and "endDate" in c:
                parts.append(f"{a}.{f} BETWEEN '{_lit(c['startDate'])}' AND '{_lit(c['endDate'])}'")
            elif "startDate" in c:
                parts.append(f"{a}.{f} >= '{_lit(c['startDate'])}'")
            elif "value" in c:
                parts.append(f"{a}.{f} LIKE '%{_like_lit(c['value'])}%' ESCAPE '\\'")
        if parts:
            sql += " WHERE " + " AND ".join(parts)

    if not is_sqlite():
        sql += " OPTION (RECOMPILE)"

    with get_connection() as conn:
        df = read_sql(sql, conn)

    # Friendly column titles: result__formatted_entry -> "Formatted Entry"
    df.columns = [c.split("__")[-1].replace("_", " ").title() for c in df.columns]
    return df, sql


def _json_safe_rows(df, limit: int):
    """Convert a DataFrame to JSON-safe rows: NaT/NaN -> null, datetimes -> ISO."""
    head = df.head(limit).copy()
    for col in head.select_dtypes(include=["datetime", "datetimetz"]).columns:
        head[col] = head[col].dt.strftime("%Y-%m-%d %H:%M:%S")
    return json.loads(head.to_json(orient="values", date_format="iso"))


def run_nl_sql(prompt: str, limit: int = 100) -> dict | None:
    """Parse + run. Returns a structured result, or None if not a data query."""
    pq = parse_prompt(prompt)
    if not pq["fields"]:
        return None
    try:
        df, sql = generate_sql(pq, limit=limit)
    except ValueError:
        return None  # not a usable data query (e.g. pruned to nothing)
    return {
        "sql": sql,
        "columns": list(df.columns),
        "rows": _json_safe_rows(df, limit),
        "row_count": int(len(df)),
    }
