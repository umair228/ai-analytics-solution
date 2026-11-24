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
from datetime import datetime
from zoneinfo import ZoneInfo

# An INNERMOST macro: its argument holds no other macro. Rendering substitutes these
# repeatedly, so macros compose — {{month:{{local:created_at}}}} buckets the local time.
MACRO_RE = re.compile(r"\{\{\s*(\w+)\s*:\s*((?:(?!\{\{).)+?)\s*\}\}", re.DOTALL)
_NAME_RE = re.compile(r"\{\{\s*(\w+)\s*:")

MACROS = ("num", "table", "days_ago", "hours_between",
          # dates (P1): buckets, local time, calendar-day differences
          "day", "week", "month", "local", "days_between",
          # row scope: "(1 = 1)" for an all-scope caller, a key filter otherwise
          "scope")

# Macros whose rendering depends on the semantic layer's `timezone:`.
TIMEZONE_MACROS = ("local",)

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


def fixed_offset_minutes(tz_name: str) -> int | None:
    """The zone's UTC offset in minutes when it never changes (no DST), else None.

    Riyadh is UTC+3 all year, so local time is a fixed shift: SWITCHOFFSET on MSSQL,
    which is about nine times cheaper than AT TIME ZONE on the PMSv2 copy. A zone with
    DST needs a real time-zone conversion, which not every dialect can do.
    """
    zone = ZoneInfo(tz_name)
    jan = datetime(2025, 1, 15, 12, tzinfo=zone).utcoffset()
    jul = datetime(2025, 7, 15, 12, tzinfo=zone).utcoffset()
    return int(jan.total_seconds() // 60) if jan == jul else None


def _offset_text(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def local_time(expr: str, dialect: str, tz: str | None, tz_mssql: str = "") -> str | None:
    """``expr`` (a UTC timestamp; naive values are read as UTC) as local wall-clock time
    in ``tz``. None when the dialect cannot express it — the caller leaves the macro
    intact so the database rejects it loudly rather than answering in UTC."""
    if not tz:
        return None
    d = (dialect or "").lower()
    offset = fixed_offset_minutes(tz)
    if d == "mssql":
        if offset is not None:
            return (f"CAST(SWITCHOFFSET(CAST({expr} AS datetimeoffset), "
                    f"'{_offset_text(offset)}') AS datetime2)")
        if not tz_mssql:
            return None        # AT TIME ZONE needs the Windows zone name
        return f"CAST(CAST({expr} AS datetimeoffset) AT TIME ZONE '{tz_mssql}' AS datetime2)"
    if d == "postgres":
        if offset is not None:
            return f"(CAST({expr} AS timestamp) + INTERVAL '{offset} minutes')"
        return f"((CAST({expr} AS timestamp) AT TIME ZONE 'UTC') AT TIME ZONE '{tz}')"
    if d == "sqlite":
        return None if offset is None else f"datetime({expr}, '{offset:+d} minutes')"
    if d == "mysql":
        if offset is not None:
            return f"DATE_ADD({expr}, INTERVAL {offset} MINUTE)"
        return f"CONVERT_TZ({expr}, '+00:00', '{tz}')"
    if d == "oracle":
        if offset is not None:
            return f"({expr} + NUMTODSINTERVAL({offset}, 'MINUTE'))"
        return f"CAST(FROM_TZ(CAST({expr} AS TIMESTAMP), 'UTC') AT TIME ZONE '{tz}' AS TIMESTAMP)"
    if d == "databricks":
        return f"from_utc_timestamp({expr}, '{tz}')"
    if d == "snowflake":
        return f"CONVERT_TIMEZONE('UTC', '{tz}', {expr})"
    return None if offset is None else f"datetime({expr}, '{offset:+d} minutes')"


def day_bucket(expr: str, dialect: str) -> str:
    """The calendar date of ``expr``."""
    d = (dialect or "").lower()
    return {"mssql": f"CAST({expr} AS date)", "postgres": f"CAST({expr} AS date)",
            "mysql": f"DATE({expr})", "oracle": f"TRUNC({expr})",
            "databricks": f"to_date({expr})", "snowflake": f"TO_DATE({expr})",
            }.get(d, f"date({expr})")


def week_bucket(expr: str, dialect: str) -> str:
    """The Monday that starts the ISO week of ``expr``, as a date.

    MSSQL's weekday numbering depends on the session's @@DATEFIRST; adding it back
    makes "days since Monday" the same under every language setting.
    """
    d = (dialect or "").lower()
    if d == "mssql":
        return (f"DATEADD(day, -((DATEPART(weekday, {expr}) + @@DATEFIRST + 5) % 7), "
                f"CAST({expr} AS date))")
    if d == "postgres":
        return f"CAST(date_trunc('week', {expr}) AS date)"
    if d == "mysql":
        return f"DATE_SUB(DATE({expr}), INTERVAL WEEKDAY({expr}) DAY)"
    if d == "oracle":
        return f"TRUNC({expr}, 'IW')"
    if d == "databricks":
        return f"CAST(date_trunc('WEEK', {expr}) AS DATE)"
    if d == "snowflake":
        return f"CAST(DATE_TRUNC('week', {expr}) AS DATE)"
    return f"date({expr}, '-6 days', 'weekday 1')"       # the Monday on or before


def month_bucket(expr: str, dialect: str) -> str:
    """The first day of the month of ``expr``, as a date."""
    d = (dialect or "").lower()
    if d == "mssql":
        return f"DATEFROMPARTS(YEAR({expr}), MONTH({expr}), 1)"
    if d == "postgres":
        return f"CAST(date_trunc('month', {expr}) AS date)"
    if d == "mysql":
        return f"DATE(DATE_FORMAT({expr}, '%Y-%m-01'))"
    if d == "oracle":
        return f"TRUNC({expr}, 'MM')"
    if d == "databricks":
        return f"trunc({expr}, 'MM')"
    if d == "snowflake":
        return f"CAST(DATE_TRUNC('month', {expr}) AS DATE)"
    return f"date({expr}, 'start of month')"


def days_between(start: str, end: str, dialect: str) -> str:
    """Whole calendar days from ``start`` to ``end`` (dates, not 24-hour periods)."""
    d = (dialect or "").lower()
    if d in ("mssql", "snowflake"):
        return f"DATEDIFF(day, {start}, {end})"
    if d == "postgres":
        return f"(CAST({end} AS date) - CAST({start} AS date))"
    if d == "mysql":
        return f"DATEDIFF({end}, {start})"
    if d == "oracle":
        return f"(TRUNC({end}) - TRUNC({start}))"
    if d == "databricks":
        return f"datediff({end}, {start})"
    return f"CAST(julianday(date({end})) - julianday(date({start})) AS INTEGER)"


_SCOPE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def scope_filter(column: str, scope) -> str:
    """Row scope for a metric on a project-scoped view.

    ``scope`` None means an all-scope caller: the filter is a tautology, and the
    macro's job is only to mark *where* a scope applies, so the loader can refuse a
    metric on a scoped view that has no such marker. A list restricts the rows to
    those keys (an empty list matches nothing).
    """
    if scope is None:
        return "(1 = 1)"
    keys = [str(k) for k in scope]
    if not keys:
        return "(1 = 0)"
    bad = [k for k in keys if not _SCOPE_KEY_RE.match(k)]
    if bad:
        raise ValueError(f"unsafe scope key(s): {bad[:3]}")
    return f"({column} IN ({', '.join(repr(k) for k in keys)}))"


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
    return sorted({m.group(1) for m in _NAME_RE.finditer(sql or "")
                   if m.group(1) not in MACROS})


def tables_read(sql: str) -> list[str]:
    """The names inside ``{{table:…}}`` macros: what a metric reads, for the scope
    and classification checks at load time."""
    return sorted({m.group(2).strip() for m in MACRO_RE.finditer(sql or "")
                   if m.group(1) == "table"})


def _split_pair(arg: str) -> tuple[str, str]:
    """Split "a, b" at the first comma outside parentheses and quotes, so the
    arguments of a two-argument macro may themselves be rendered macros."""
    depth, quote = 0, None
    for i, ch in enumerate(arg):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            return arg[:i].strip(), arg[i + 1:].strip()
    return arg.strip(), ""


def uses_macro(sql: str, name: str) -> bool:
    return any(m.group(1) == name for m in _NAME_RE.finditer(sql or ""))


def render(sql: str, datasource, *, timezone: str | None = None, timezone_mssql: str = "",
           scope=None) -> str:
    """Render portable metric SQL for the dialect of ``datasource``.

    ``timezone`` is the semantic layer's IANA zone (``{{local:…}}``); ``scope`` is
    None for an all-scope caller or the keys a scoped caller may see
    (``{{scope:…}}``). Innermost macros are substituted first, so they compose.
    """
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
            start, end = _split_pair(arg)
            return hours_between(start, end, dialect)
        if kind == "days_between":
            start, end = _split_pair(arg)
            return days_between(start, end, dialect)
        if kind == "day":
            return day_bucket(arg, dialect)
        if kind == "week":
            return week_bucket(arg, dialect)
        if kind == "month":
            return month_bucket(arg, dialect)
        if kind == "local":
            rendered = local_time(arg, dialect, timezone, timezone_mssql)
            return m.group(0) if rendered is None else rendered
        if kind == "scope":
            return scope_filter(arg, scope)
        return m.group(0)  # unknown — left intact so it fails loudly, not silently

    text = sql or ""
    while True:
        rendered = MACRO_RE.sub(_sub, text)
        if rendered == text:
            return rendered.strip()
        text = rendered
