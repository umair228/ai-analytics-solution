"""Dialect-portable SQL fragments for the replica + marts.

The source is the LIMS (usually MSSQL); the target replica is Postgres in prod
and SQLite in dev/tests. The two fragments that differ and matter for accuracy
are the safe text→float cast (LabWare stores NUMERIC_ENTRY as text) and identifier
quoting. Everything here is pure (unit-tested in replication/tests.py).
"""
from __future__ import annotations

from .tables import STATUS_LABELS


def quote_ident(name: str, dialect: str) -> str:
    if dialect == "mssql":
        return f"[{name}]"
    if dialect in ("mysql", "databricks"):
        return f"`{name}`"                # Spark SQL uses backticks
    return f'"{name}"'  # postgres / sqlite / oracle / snowflake — ANSI double quotes


def table_ref(table: str, schema: str | None, dialect: str) -> str:
    t = quote_ident(table, dialect)
    return f"{quote_ident(schema, dialect)}.{t}" if schema else t


def safe_float(expr: str, dialect: str) -> str:
    """Convert a possibly-non-numeric text column to a float, NULL if it isn't a
    clean number — the portable equivalent of MSSQL ``TRY_CONVERT(float, …)``."""
    if dialect == "postgres":
        return (f"CASE WHEN {expr} ~ '^\\s*-?\\d+(\\.\\d+)?\\s*$' "
                f"THEN CAST({expr} AS double precision) END")
    if dialect == "mssql":
        return f"TRY_CONVERT(float, {expr})"
    if dialect == "databricks":
        return f"try_cast({expr} AS DOUBLE)"
    if dialect == "snowflake":
        return f"TRY_TO_DOUBLE({expr})"
    if dialect == "sqlite":
        # SQLite has no REGEXP built in, and a bare CAST is actively WRONG here:
        # CAST('<0.05' AS REAL) is 0.0, not NULL. LabWare is full of censored values
        # ('<0.05', '>100', 'ND'), and reading one as zero drags an average down and
        # can turn an off-spec batch into a passing one — it lands silently in
        # mart_results.value. GLOB gives a real guard: only digits/dot/minus, and at
        # least one digit. Keep this in step with semantics.metric_sql.safe_number.
        return (f"CASE WHEN {expr} IS NOT NULL "
                f"AND TRIM({expr}) NOT GLOB '*[^0-9.-]*' "
                f"AND TRIM({expr}) GLOB '*[0-9]*' "
                f"THEN CAST(TRIM({expr}) AS REAL) END")
    return f"CAST({expr} AS REAL)"


def status_case(col: str, dialect: str) -> str:
    whens = " ".join(f"WHEN '{code}' THEN '{label}'"
                     for code, label in STATUS_LABELS.items())
    return f"CASE {col} {whens} ELSE {col} END"


def is_off_spec(col: str) -> str:
    """1 when the IN_SPEC flag is 'F' (off-spec), else 0 — portable everywhere."""
    return f"CASE WHEN {col} = 'F' THEN 1 ELSE 0 END"
