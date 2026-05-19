"""Executes compiled queries and raw SQL with read-only safety rails."""
import re

import sqlparse
from django.conf import settings
from sqlalchemy import text
from sqlparse import tokens as T

from connections.engine import get_engine
from core.serialization import jsonable_rows

from .compiler import compile_spec


class QueryError(Exception):
    """Raised when a query is rejected or fails to execute."""


def assert_read_only(sql: str) -> bool:
    """Reject anything that is not a single read-only SELECT / WITH statement."""
    if not sql or not sql.strip():
        raise QueryError("Empty SQL statement.")

    statements = [s for s in sqlparse.parse(sql) if str(s).strip().strip(";")]
    if len(statements) != 1:
        raise QueryError("Exactly one SQL statement is allowed.")

    statement = statements[0]
    first = statement.token_first(skip_cm=True)
    first_keyword = first.normalized.upper() if first else ""
    if statement.get_type() != "SELECT" and first_keyword != "WITH":
        raise QueryError("Only read-only SELECT / WITH queries are allowed.")

    for token in statement.flatten():
        if token.ttype is T.DML and token.normalized.upper() != "SELECT":
            raise QueryError(f"Forbidden operation: {token.normalized.upper()}")
        if token.ttype is T.DDL:
            raise QueryError(f"Forbidden operation: {token.normalized.upper()}")

    if re.search(r"\bINTO\b", sql, re.IGNORECASE):
        raise QueryError("SELECT ... INTO is not permitted.")
    return True


def _result_payload(result, sql, max_rows):
    columns = list(result.keys())
    fetched = result.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = jsonable_rows(fetched[:max_rows])
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "sql": sql,
    }


def execute_spec(datasource, raw_spec, database=None):
    """Compile and execute a query spec. Returns a result payload."""
    compiled = compile_spec(datasource, raw_spec, database)
    engine = get_engine(datasource, database)
    try:
        with engine.connect() as conn:
            result = conn.execute(compiled.statement)
            return _result_payload(result, compiled.sql, settings.DSE_QUERY_MAX_ROWS)
    except Exception as exc:  # noqa: BLE001
        raise QueryError(str(exc)) from exc


def execute_raw_sql(datasource, sql, database=None, params=None):
    """Execute a hand-written SQL statement after enforcing read-only rules.

    params: optional dict of named bind-parameters, e.g. {"start_date": "2024-01-01"}.
    Use :name syntax in SQL: SELECT * FROM t WHERE date >= :start_date
    """
    assert_read_only(sql)
    engine = get_engine(datasource, database)
    try:
        with engine.connect() as conn:
            stmt = text(sql)
            result = conn.execute(stmt, params or {})
            return _result_payload(result, sql, settings.DSE_QUERY_MAX_ROWS)
    except QueryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise QueryError(str(exc)) from exc
