"""Pure, dependency-free SQL helpers for the text-to-SQL slice.

No Django / DB / LLM imports here on purpose — everything is unit-testable
(see ai/tests_texttosql.py). Dialects are referenced by the same string values
as :class:`connections.constants.SourceType` (``mssql``/``postgres``/…).
"""
from __future__ import annotations

import re

MSSQL = "mssql"
ORACLE = "oracle"
POSTGRES = "postgres"
MYSQL = "mysql"
SQLITE = "sqlite"
ODBC = "odbc"

# Already-bounded markers — anchored so we don't false-match "top"/"limit" inside
# a string literal or comment (which would skip the cap entirely).
_LEAD_TOP_RE = re.compile(r"^\s*select\s+(distinct\s+)?top\b", re.IGNORECASE | re.DOTALL)
_TRAILING_LIMIT_RE = re.compile(r"\blimit\s+\d+\s*(offset\s+\d+\s*)?$", re.IGNORECASE)
_TRAILING_FETCH_RE = re.compile(r"\bfetch\s+(first|next)\b.*\brows?\b\s*(only)?\s*$",
                                re.IGNORECASE | re.DOTALL)
_SETOP_RE = re.compile(r"\b(union|except|intersect)\b", re.IGNORECASE)
_LEAD_SELECT_RE = re.compile(r"^\s*select\s+(distinct\s+)?", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"```(?:sql)?\s*(.+?)```", re.IGNORECASE | re.DOTALL)
_FIRST_STMT_RE = re.compile(r"\b(with|select)\b", re.IGNORECASE | re.DOTALL)


def extract_sql(text: str) -> str:
    """Pull a single SQL statement out of a model response.

    Strips a ```sql ...``` fence if present, drops any leading prose before the
    first SELECT/WITH, and keeps only the first statement (up to the first ';').
    Returns "" when nothing SQL-like is found (the caller then rejects it).
    """
    if not text:
        return ""
    t = text.strip()
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    m = _FIRST_STMT_RE.search(t)
    if not m:
        return ""  # no SELECT/WITH anywhere -> not a SQL statement
    t = t[m.start():].split(";")[0].strip()
    return t


def inject_row_limit(sql: str, source_type: str, n: int) -> str:
    """Cap the number of rows the database itself produces, dialect-aware.

    MSSQL → ``SELECT [DISTINCT] TOP n …`` (only for a plain leading SELECT; CTE/
    WITH queries are left alone and rely on the executor's fetch cap). Oracle →
    ``FETCH FIRST n ROWS ONLY``. Postgres/MySQL/SQLite → ``LIMIT n``. Unknown
    dialects (ODBC) are left unchanged. No-ops if the query is already bounded.
    """
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return s
    n = int(n)
    st = (source_type or "").lower()
    if st == MSSQL:
        if _LEAD_TOP_RE.match(s):                       # already TOP-bounded
            return s
        # CTE and set-ops: TOP would bind to the wrong (sub)query, so leave it —
        # the executor's fetchmany cap still bounds the rows returned to the app.
        if s.lower().startswith("with") or _SETOP_RE.search(s):
            return s
        m = _LEAD_SELECT_RE.match(s)
        if m:  # SELECT ... / SELECT DISTINCT ... -> insert TOP after any DISTINCT
            return s[:m.end()] + f"TOP {n} " + s[m.end():]
        return s
    if st == ORACLE:
        if _TRAILING_FETCH_RE.search(s):
            return s
        return s + f"\nFETCH FIRST {n} ROWS ONLY"
    if st in (POSTGRES, MYSQL, SQLITE):
        if _TRAILING_LIMIT_RE.search(s) or _TRAILING_FETCH_RE.search(s):
            return s
        return s + f"\nLIMIT {n}"
    return s  # ODBC / unknown — don't risk an invalid clause


def dialect_label(source_type: str) -> str:
    """A short human label + the cap idiom, for the generator prompt."""
    st = (source_type or "").lower()
    return {
        MSSQL: "Microsoft SQL Server (T-SQL; use TOP, TRY_CONVERT, GETDATE, DATEDIFF)",
        POSTGRES: "PostgreSQL (use LIMIT, ::numeric casts, NOW())",
        MYSQL: "MySQL (use LIMIT, CAST, NOW())",
        ORACLE: "Oracle (use FETCH FIRST n ROWS ONLY, TO_NUMBER, SYSDATE)",
        SQLITE: "SQLite (use LIMIT, CAST)",
        ODBC: "generic SQL",
    }.get(st, "generic SQL")
