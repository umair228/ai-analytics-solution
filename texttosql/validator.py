"""Deterministic gates that make generated SQL trustworthy — independent of model
quality (DIS_TalkToData_Plan.md §4/§8). Three layers:

  1. read-only        — reuse querybuilder.assert_read_only (DML/DDL/multi-stmt → reject).
  2. name allow-list  — every table referenced in FROM/JOIN must be one the model was
                        actually shown (or a CTE it defined). Deterministically kills
                        the "invented a plausible table" hallucination.
  3. shape sanity     — reject GROUP BY on a near-empty do_not_group column (the
                        "out-of-spec by instrument" trap).

Plus post-execution result sanity (0-row aggregate, all-NULL value column) that
triggers a repair instead of returning a confidently-wrong answer.

Identifier extraction is regex-based on the (already read-only, single-statement)
SQL — robust for the generated SELECT shapes here. A stricter AST extractor
(sqlglot) is a drop-in future hardening; the gates degrade safe either way because
anything they miss is still caught by the DB error → repair loop.
"""
from __future__ import annotations

import re

from querybuilder.executor import QueryError, assert_read_only

# An identifier: bare, [bracketed] or "quoted".
_IDENT = r'(\[[^\]]+\]|"[^"]+"|[a-zA-Z_]\w*)'
# A relation introduced by FROM / JOIN / CROSS|OUTER APPLY (optionally schema-qualified).
_REL_KW_RE = re.compile(
    rf"\b(?:from|join|cross\s+apply|outer\s+apply)\s+{_IDENT}(?:\.{_IDENT})?",
    re.IGNORECASE)
_FROM_RE = re.compile(r"\bfrom\b", re.IGNORECASE)
# Where a FROM-list ends (so we can split comma-joins only within the FROM list).
_FROM_END_RE = re.compile(
    r"\b(join|cross\s+apply|outer\s+apply|where|group\s+by|having|order\s+by|"
    r"union|except|intersect|limit|offset|fetch|on)\b", re.IGNORECASE)
_LEAD_IDENT_RE = re.compile(rf"^\s*{_IDENT}(?:\.{_IDENT})?")
_CTE_RE = re.compile(r"([a-zA-Z_]\w*)\s+as\s*\(", re.IGNORECASE)
_GROUPBY_RE = re.compile(
    r"\bgroup\s+by\b(.+?)(?:\border\s+by\b|\bhaving\b|\blimit\b|\bfetch\b|$)",
    re.IGNORECASE | re.DOTALL)
_AGG_RE = re.compile(r"\b(count|sum|avg|min|max)\s*\(|\bgroup\s+by\b", re.IGNORECASE)
_VALUE_COL_RE = re.compile(r"value|numeric|avg|sum|min|max|rate|pct|percent|mean", re.IGNORECASE)


# Functions that use FROM/FOR/IN *inside* their parens (not a table clause).
_FUNC_FROM_RE = re.compile(r"\b(extract|substring|trim|position|overlay)\s*\([^()]*\)",
                           re.IGNORECASE)
_STRLIT_RE = re.compile(r"'(?:''|[^'])*'")


def _clean(sql: str) -> str:
    """Strip string literals and FROM-using function calls so the table/group-by
    regexes don't read e.g. EXTRACT(EPOCH FROM col) or 'from x' as a real table."""
    s = _STRLIT_RE.sub(" ", sql or "")
    s = _FUNC_FROM_RE.sub(" ", s)
    return s


def _unquote(tok: str) -> str:
    return tok.strip().strip('[]"`').lower()


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are NOT inside parentheses (so a subquery / function
    arg list in the FROM clause isn't mistaken for multiple relations)."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def referenced_tables(sql: str) -> set[str]:
    """Lower-cased table names in FROM/JOIN/APPLY (incl. comma-joins and bracketed/
    quoted identifiers; schema prefix stripped). Subqueries contribute their inner
    tables, not the derived alias."""
    s = _clean(sql)
    out = set()
    # FROM/JOIN/APPLY-introduced relations (and the first FROM target).
    for m in _REL_KW_RE.finditer(s):
        out.add(_unquote(m.group(2) or m.group(1)))
    # Additional comma-separated relations within each FROM-list.
    for fm in _FROM_RE.finditer(s):
        tail = s[fm.end():]
        end = _FROM_END_RE.search(tail)
        span = tail[:end.start()] if end else tail
        for piece in _split_top_commas(span):
            p = piece.strip()
            if not p or p.startswith("("):   # subquery / parenthesised — skip
                continue
            lm = _LEAD_IDENT_RE.match(p)
            if lm:
                out.add(_unquote(lm.group(2) or lm.group(1)))
    return out


def cte_names(sql: str) -> set[str]:
    """CTE names defined by a leading WITH — these are legal FROM targets."""
    if not re.match(r"^\s*with\b", sql or "", re.IGNORECASE):
        return set()
    return {m.group(1).lower() for m in _CTE_RE.finditer(_clean(sql))}


def groupby_columns(sql: str) -> list[str]:
    """Lower-cased column names in the GROUP BY (last dotted segment; positional refs ignored)."""
    m = _GROUPBY_RE.search(_clean(sql))
    if not m:
        return []
    cols = []
    for part in m.group(1).split(","):
        token = re.split(r"\s+", part.strip())[0] if part.strip() else ""
        token = token.split(".")[-1].strip('[]"`')
        if token and not token.isdigit():
            cols.append(token.lower())
    return cols


def validate_sql(sql: str, schema) -> list[str]:
    """Return a list of violations (empty = ok). ``schema`` is a SchemaContext."""
    try:
        assert_read_only(sql)
    except QueryError as exc:
        return [f"not a read-only single SELECT: {exc}"]

    violations = []
    allowed = {t.lower() for t in schema.table_names()} | cte_names(sql)
    unknown = sorted(t for t in referenced_tables(sql) if t not in allowed)
    if unknown:
        violations.append(
            "Unknown table(s) " + ", ".join(unknown) + " — use ONLY these tables: "
            + ", ".join(sorted(schema.table_names())) + ".")

    dng = {c.lower() for c in schema.do_not_group}
    bad = sorted({c for c in groupby_columns(sql) if c in dng})
    if bad:
        violations.append(
            "Do NOT GROUP BY " + ", ".join(bad) + " — that column is (near) empty in "
            "this data and yields a meaningless bucket. Group by product, analysis or "
            "ENTERED_BY (operator) instead.")
    return violations


def is_aggregate(sql: str) -> bool:
    return bool(_AGG_RE.search(sql or ""))


def sanity_violations(sql: str, payload: dict) -> list[str]:
    """Post-execution checks that warrant a repair attempt (not a hard failure)."""
    out = []
    rows = payload.get("rows") or []
    cols = payload.get("columns") or []

    if is_aggregate(sql) and not rows:
        out.append(
            "The query returned 0 rows for an aggregate — likely a wrong filter or "
            "encoding (e.g. IN_SPEC compared to a number instead of 'T'/'F', or a value "
            "column not cast with TRY_CONVERT). Re-check filters and encodings.")
        return out

    if rows:
        for ci, name in enumerate(cols):
            if not _VALUE_COL_RE.search(str(name)):
                continue
            if all((r[ci] if ci < len(r) else None) is None for r in rows):
                out.append(
                    f"Column '{name}' is entirely NULL — likely a missing numeric cast. "
                    "Wrap the source column in TRY_CONVERT(float, …) (and filter NOT NULL).")
                break
    return out
