"""Denormalized analytics marts, built ON the replica from the raw mirror.

Each mart bakes the LabWare quirks once (the text→float cast, the IN_SPEC and
STATUS decodes, the SAMPLE↔RESULT join) into clean, correctly-typed columns, so
downstream text-to-SQL and dashboards reason over sane data instead of re-deriving
the conventions every query. Target dialect is Postgres (prod) or SQLite (dev).
"""
from __future__ import annotations

from sqlalchemy import text

from .sql_dialect import is_off_spec, quote_ident, safe_float, status_case, table_ref


def _hours_between(start: str, end: str, dialect: str) -> str:
    if dialect == "postgres":
        return f"EXTRACT(EPOCH FROM (CAST({end} AS timestamp) - CAST({start} AS timestamp))) / 3600.0"
    if dialect == "sqlite":
        return f"(julianday({end}) - julianday({start})) * 24.0"
    return "NULL"


def mart_definitions(dialect: str, schema: str | None) -> list[tuple[str, str]]:
    """Return [(mart_table_name, SELECT sql), ...] for the target dialect."""
    S = table_ref("SAMPLE", schema, dialect)
    R = table_ref("RESULT", schema, dialect)

    def c(alias, name):  # qualified, quoted column reference
        return f"{alias}.{quote_ident(name, dialect)}"

    results = f"""
SELECT
  {c('r', 'SAMPLE_NUMBER')} AS sample_number,
  {c('r', 'RESULT_NUMBER')} AS result_number,
  {c('s', 'TEXT_ID')} AS text_id,
  {c('s', 'PRODUCT')} AS product,
  {c('r', 'ANALYSIS')} AS analysis,
  {c('r', 'NAME')} AS component,
  {safe_float(c('r', 'NUMERIC_ENTRY'), dialect)} AS value,
  {c('r', 'UNITS')} AS units,
  {c('r', 'IN_SPEC')} AS in_spec,
  {is_off_spec(c('r', 'IN_SPEC'))} AS is_off_spec,
  {c('r', 'ENTERED_BY')} AS entered_by,
  {c('r', 'ENTERED_ON')} AS entered_on
FROM {R} AS r
JOIN {S} AS s ON {c('r', 'SAMPLE_NUMBER')} = {c('s', 'SAMPLE_NUMBER')}
WHERE {c('r', 'NUMERIC_ENTRY')} IS NOT NULL
""".strip()

    samples = f"""
SELECT
  {c('s', 'SAMPLE_NUMBER')} AS sample_number,
  {c('s', 'TEXT_ID')} AS text_id,
  {c('s', 'PRODUCT')} AS product,
  {c('s', 'SAMPLE_TYPE')} AS sample_type,
  {c('s', 'STATUS')} AS status_code,
  {status_case(c('s', 'STATUS'), dialect)} AS status_label,
  {c('s', 'IN_SPEC')} AS in_spec,
  {is_off_spec(c('s', 'IN_SPEC'))} AS is_off_spec,
  {c('s', 'LOGIN_DATE')} AS login_date,
  {c('s', 'DATE_COMPLETED')} AS date_completed,
  {_hours_between(c('s', 'LOGIN_DATE'), c('s', 'DATE_COMPLETED'), dialect)} AS tat_hours
FROM {S} AS s
""".strip()

    offspec = f"""
SELECT
  {c('r', 'SAMPLE_NUMBER')} AS sample_number,
  {c('r', 'RESULT_NUMBER')} AS result_number,
  {c('s', 'TEXT_ID')} AS text_id,
  {c('s', 'PRODUCT')} AS product,
  {c('r', 'ANALYSIS')} AS analysis,
  {c('r', 'NAME')} AS component,
  {safe_float(c('r', 'NUMERIC_ENTRY'), dialect)} AS value,
  {c('r', 'UNITS')} AS units,
  {c('r', 'ENTERED_BY')} AS entered_by,
  {c('r', 'ENTERED_ON')} AS entered_on
FROM {R} AS r
JOIN {S} AS s ON {c('r', 'SAMPLE_NUMBER')} = {c('s', 'SAMPLE_NUMBER')}
WHERE {c('r', 'IN_SPEC')} = 'F'
""".strip()

    return [
        ("mart_results", results),
        ("mart_samples", samples),
        ("mart_offspec", offspec),
    ]


def build_marts(target_engine, dialect: str, schema: str | None = None) -> list[dict]:
    """(Re)build every mart on the target. Returns [{mart, rows}, ...]."""
    built = []
    with target_engine.begin() as conn:
        for name, select in mart_definitions(dialect, schema):
            ref = table_ref(name, schema, dialect)
            conn.execute(text(f"DROP TABLE IF EXISTS {ref}"))
            conn.execute(text(f"CREATE TABLE {ref} AS {select}"))
            rows = conn.execute(text(f"SELECT COUNT(*) FROM {ref}")).scalar()
            built.append({"mart": name, "rows": int(rows or 0)})
    return built
