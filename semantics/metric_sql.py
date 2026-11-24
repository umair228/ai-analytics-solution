"""Render a portable certified-metric definition into dialect-specific SQL.

A certified metric is the deterministic fast path: curated, QA-approved SQL that
answers a hot question without the LLM writing anything. It therefore has to run
against **both** the LabWare MSSQL source and the Postgres/SQLite replica, because
which one the agent points at is a deployment choice, not a semantic one.

It didn't. The metrics were hardcoded T-SQL (``TOP 50``, ``TRY_CONVERT``, ``dbo.``),
so against a replica every one of them raised — and ``texttosql.runner`` caught the
error and quietly fell through to the LLM. The two headline features, the certified
fast path and the replica, silently excluded each other: turning either on turned
the other off, and nothing said so.

Portability rests on three things:

1. **Unquoted identifiers.** Metric SQL writes ``s.PRODUCT``, never ``s."PRODUCT"``.
   The convention survives every dialect DIS supports, for different reasons in each:

   * **MSSQL / SQLite / Databricks (Spark)** — identifiers are case-insensitive.
   * **Postgres** — folds unquoted identifiers to *lower* case, which resolves because
     ``replication.loader._canon`` deliberately creates the replica lower case.
   * **Snowflake** — folds unquoted identifiers to *upper* case, which resolves against
     the upper-case objects Snowflake itself creates from unquoted DDL.

   One SQL, five dialects. ⚠ The one schema that would break it is a Snowflake
   warehouse whose tables were created with *quoted lower-case* names (``"sample"``);
   that is unusual, and it is the customer's DDL convention rather than something we
   can paper over.
2. **``{{num:expr}}``** — the text→number cast. LabWare stores NUMERIC_ENTRY as
   text and every dialect spells the safe cast differently.
3. **``{{table:NAME}}``** for the schema prefix (``dbo.`` on MSSQL, nothing on the
   replica), and a ``limit:`` field on the metric instead of ``TOP n`` / ``LIMIT n``
   — the row cap is positional, so it can't be an inline macro. ``inject_row_limit``
   already places it correctly per dialect.

Everything else in a metric is plain ANSI and needs no help.
"""
from __future__ import annotations

import re

MACRO_RE = re.compile(r"\{\{\s*(\w+)\s*:\s*(.+?)\s*\}\}", re.DOTALL)

MACROS = ("num", "table", "days_ago", "hours_between")

# Same numeric guard as replication.sql_dialect.safe_float, deliberately: a value
# is either a number everywhere in DIS or nowhere. LabWare entries like "<0.05",
# "ND" or "" must land as NULL, not 0 — a censored result silently read as zero is
# how an off-spec batch passes. Note this also excludes E-notation; if that ever
# matters, change BOTH this and replication.sql_dialect together.
_PG_NUMERIC_GUARD = r"^\s*-?\d+(\.\d+)?\s*$"
_MYSQL_NUMERIC_GUARD = r"^[[:space:]]*-?[0-9]+(\\.[0-9]+)?[[:space:]]*$"


def safe_number(expr: str, dialect: str) -> str:
    """A text column → number, NULL when it isn't cleanly numeric.

    NB Postgres casts to ``numeric``, not ``double precision``. ``ROUND(v, n)`` has
    no double-precision overload in Postgres, and the metrics round — so casting to
    double here would make every ``ROUND(AVG(...), 4)`` metric fail on the replica
    with "function round(double precision, integer) does not exist". This is exactly
    the class of breakage the old silent fall-through hid.
    """
    d = (dialect or "").lower()
    if d == "mssql":
        return f"TRY_CONVERT(float, {expr})"
    if d == "postgres":
        return (f"CASE WHEN {expr} ~ '{_PG_NUMERIC_GUARD}' "
                f"THEN CAST({expr} AS numeric) END")
    if d == "sqlite":
        # SQLite has no REGEXP built in, and a bare CAST is actively dangerous:
        # CAST('<0.05' AS REAL) is 0.0, not NULL. A censored LIMS result read as
        # zero drags an average down and can turn an off-spec batch into a passing
        # one. GLOB gets us a real guard: allow only digits/dot/minus, and require
        # at least one digit — so '<0.05', '>100', 'ND', 'N/A' and '' all → NULL.
        return (f"CASE WHEN {expr} IS NOT NULL "
                f"AND TRIM({expr}) NOT GLOB '*[^0-9.-]*' "
                f"AND TRIM({expr}) GLOB '*[0-9]*' "
                f"THEN CAST(TRIM({expr}) AS REAL) END")
    if d == "mysql":
        return (f"CASE WHEN {expr} REGEXP '{_MYSQL_NUMERIC_GUARD}' "
                f"THEN CAST({expr} AS DECIMAL(38,10)) END")
    if d == "oracle":
        return f"TO_NUMBER({expr} DEFAULT NULL ON CONVERSION ERROR)"
    if d == "databricks":
        # Spark's try_cast yields NULL rather than raising on '<0.05' / 'ND', and
        # round(double, n) is fine here — Spark has no Postgres-style overload gap.
        return f"try_cast({expr} AS DOUBLE)"
    if d == "snowflake":
        return f"TRY_TO_DOUBLE({expr})"
    return f"CAST({expr} AS REAL)"


def days_ago(n, dialect: str) -> str:
    """A date N days back — "samples logged in the last 30 days" and friends.

    Every dialect spells "now minus N days" differently, and getting it wrong is
    the kind of error that silently returns *some* plausible number.
    """
    n = int(n)
    d = (dialect or "").lower()
    if d == "mssql":
        return f"DATEADD(day, -{n}, CAST(GETDATE() AS date))"
    if d == "postgres":
        return f"(CURRENT_DATE - INTERVAL '{n} days')"
    if d == "sqlite":
        # Text ISO timestamps compare correctly against an ISO date, lexicographically.
        return f"date('now', '-{n} days')"
    if d == "mysql":
        return f"DATE_SUB(CURDATE(), INTERVAL {n} DAY)"
    if d == "oracle":
        return f"(TRUNC(SYSDATE) - {n})"
    if d == "databricks":
        return f"date_sub(current_date(), {n})"
    if d == "snowflake":
        return f"DATEADD(day, -{n}, CURRENT_DATE())"
    return f"date('now', '-{n} days')"


def hours_between(start: str, end: str, dialect: str) -> str:
    """Elapsed hours — turnaround time, the number every lab manager is judged on.

    Two traps, both of which have bitten:

    1. **Truncation.** ``DATEDIFF(hour, …)`` on MSSQL and ``TIMESTAMPDIFF(HOUR, …)``
       on MySQL return whole hours: a sample logged 08:00 and completed 09:59 counts
       as **1 hour**, not 1.98. Postgres and SQLite return the fraction. So the SAME
       turnaround tile answered differently depending on which datasource it ran
       against — a lab moving from the LIMS source to the replica would watch its
       mean TAT change for no reason. Every dialect below now computes in SECONDS and
       divides, so the answer is the same everywhere.
    2. **Postgres has no ``ROUND(double precision, int)``**, and every TAT tile rounds
       — hence the cast to ``numeric``. (``replication.sql_dialect._hours_between``
       deliberately keeps double: it feeds a mart column, which is never rounded.)
    """
    d = (dialect or "").lower()
    if d == "mssql":
        # DATEDIFF(second) not (hour): whole-hour truncation would silently disagree
        # with every other dialect.
        return f"(DATEDIFF(second, {start}, {end}) / 3600.0)"
    if d == "postgres":
        return (f"CAST(EXTRACT(EPOCH FROM (CAST({end} AS timestamp) "
                f"- CAST({start} AS timestamp))) / 3600.0 AS numeric)")
    if d == "sqlite":
        return f"((julianday({end}) - julianday({start})) * 24.0)"
    if d == "mysql":
        return f"(TIMESTAMPDIFF(SECOND, {start}, {end}) / 3600.0)"
    if d == "oracle":
        return f"(({end} - {start}) * 24)"          # date arithmetic is already fractional
    if d == "databricks":
        return (f"((CAST(to_timestamp({end}) AS BIGINT) "
                f"- CAST(to_timestamp({start}) AS BIGINT)) / 3600.0)")
    if d == "snowflake":
        return f"(DATEDIFF(second, {start}, {end}) / 3600.0)"
    return f"((julianday({end}) - julianday({start})) * 24.0)"


def schema_for(datasource) -> str:
    """The schema to qualify metric tables with.

    Mirrors ``replication.loader._source_table_ref``: MSSQL defaults to ``dbo``;
    the replica (Postgres/SQLite) uses the connection's default schema.
    """
    explicit = (getattr(datasource, "schema_name", "") or "").strip()
    if explicit:
        return explicit
    return "dbo" if (getattr(datasource, "source_type", "") or "").lower() == "mssql" else ""


def _table_ref(name: str, schema: str) -> str:
    # Unqualified and UNQUOTED on purpose — see the module docstring.
    return f"{schema}.{name}" if schema else name


def unknown_macros(sql: str) -> list[str]:
    """Macro names in ``sql`` that this renderer does not implement.

    A typo'd macro must fail at spec-load time. Otherwise it survives rendering as
    a literal ``{{nmu:x}}``, the database rejects it, and the certified path falls
    through to the LLM — the failure looks like "the metric didn't match" rather
    than "the metric is broken".
    """
    return sorted({m.group(1) for m in MACRO_RE.finditer(sql or "")
                   if m.group(1) not in MACROS})


def render(sql: str, datasource) -> str:
    """Render portable metric SQL for the dialect of ``datasource``."""
    dialect = (getattr(datasource, "source_type", "") or "").lower()
    schema = schema_for(datasource)

    def _sub(m):
        kind, arg = m.group(1), m.group(2).strip()
        if kind == "num":
            return safe_number(arg, dialect)
        if kind == "table":
            return _table_ref(arg, schema)
        if kind == "days_ago":
            return days_ago(arg, dialect)
        if kind == "hours_between":
            start, _, end = arg.partition(",")
            return hours_between(start.strip(), end.strip(), dialect)
        return m.group(0)  # unknown — left intact so it fails loudly, not silently

    return MACRO_RE.sub(_sub, sql or "").strip()
