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
    if dialect == "mysql":
        return f"`{name}`"
    return f'"{name}"'  # postgres / sqlite / oracle — ANSI double quotes


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
    if dialect == "sqlite":
        # SQLite has no regex by default; rely on a clean-numeric guard. CAST of a
        # clean numeric string is exact; empty/NULL → NULL.
        return f"CASE WHEN {expr} IS NOT NULL AND TRIM({expr}) <> '' THEN CAST({expr} AS REAL) END"
    return f"CAST({expr} AS REAL)"


def status_case(col: str, dialect: str) -> str:
    whens = " ".join(f"WHEN '{code}' THEN '{label}'"
                     for code, label in STATUS_LABELS.items())
    return f"CASE {col} {whens} ELSE {col} END"


def is_off_spec(col: str) -> str:
    """1 when the IN_SPEC flag is 'F' (off-spec), else 0 — portable everywhere."""
    return f"CASE WHEN {col} = 'F' THEN 1 ELSE 0 END"
