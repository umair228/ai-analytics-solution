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


# ──────────────────────────────────────────────────────────────────────────
#  Server-side dashboard filtering — apply filters as real SQL predicates so
#  they work on the *full* result set (not just truncated cached rows) and on
#  any column the query outputs, with correct typing done by the database.
# ──────────────────────────────────────────────────────────────────────────

def _split_trailing_order_by(sql: str) -> tuple[str, str]:
    """Split a SELECT into (body, trailing ORDER BY) — paren-aware so we only
    catch a top-level ORDER BY. SQL Server forbids ORDER BY inside a derived
    table, so we lift it out of the subquery wrapper and re-apply it outside."""
    s = sql.rstrip().rstrip(";")
    depth, last = 0, -1
    low = s.lower()
    i = 0
    while i < len(low):
        ch = low[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and low.startswith("order by", i) and (
            i == 0 or not low[i - 1].isalnum() and low[i - 1] != "_"
        ):
            last = i
        i += 1
    if last >= 0:
        return s[:last].rstrip(), s[last:].strip()
    return s, ""


def _coerce_date_to(value: str) -> str:
    """A bare 'YYYY-MM-DD' upper bound should include the whole day."""
    v = str(value)
    return f"{v} 23:59:59.997" if len(v) == 10 and v[4] == "-" else v


def build_filter_where(filters, allowed_columns, quote) -> tuple[str, dict]:
    """Build a parameterised WHERE fragment from dashboard filter specs.

    Only columns present in ``allowed_columns`` are honoured (guards against
    injecting arbitrary identifiers). Values are always bound, never inlined.
    Returns ("a AND b", {params}) or ("", {}).
    """
    allowed = set(allowed_columns or [])
    clauses, params = [], {}
    for i, f in enumerate(filters or []):
        col = (f or {}).get("column")
        if not col or col not in allowed:
            continue
        qcol = quote(col)
        ftype = f.get("type")
        pk = f"flt{i}"

        if ftype == "date-range":
            frm, to = f.get("from"), f.get("to")
            if frm:
                clauses.append(f"{qcol} >= :{pk}_a")
                params[f"{pk}_a"] = frm
            if to:
                clauses.append(f"{qcol} <= :{pk}_b")
                params[f"{pk}_b"] = _coerce_date_to(to)
        elif ftype == "number-range":
            lo, hi = f.get("min"), f.get("max")
            if lo not in (None, ""):
                clauses.append(f"{qcol} >= :{pk}_a")
                params[f"{pk}_a"] = lo
            if hi not in (None, ""):
                clauses.append(f"{qcol} <= :{pk}_b")
                params[f"{pk}_b"] = hi
        elif ftype == "text-search":
            v = f.get("value")
            if v not in (None, ""):
                clauses.append(f"{qcol} LIKE :{pk}")
                params[pk] = f"%{v}%"
        else:  # dropdown / equality — single value or multi-select list
            values = f.get("values")
            if values is None and f.get("value") not in (None, ""):
                values = [f.get("value")]
            values = [v for v in (values or []) if v not in (None, "")]
            if len(values) == 1:
                clauses.append(f"{qcol} = :{pk}")
                params[pk] = values[0]
            elif len(values) > 1:
                names = [f"{pk}_{j}" for j in range(len(values))]
                placeholders = ", ".join(f":{n}" for n in names)
                clauses.append(f"{qcol} IN ({placeholders})")
                params.update({n: v for n, v in zip(names, values)})
    return " AND ".join(clauses), params


def execute_raw_sql_filtered(datasource, sql, database=None, params=None,
                             filters=None, columns=None):
    """Run raw SQL with optional dashboard filters applied server-side.

    ``params``  — query bind-params (e.g. pre-aggregation date params).
    ``filters`` — output-column filters, applied by wrapping the query in a
                  derived table:  SELECT * FROM (<sql>) dse_sub WHERE … ORDER BY …
    ``columns`` — the query's known output columns (the allow-list).
    """
    if not filters:
        return execute_raw_sql(datasource, sql, database, params=params)

    assert_read_only(sql)
    engine = get_engine(datasource, database)
    quote = engine.dialect.identifier_preparer.quote
    where, fparams = build_filter_where(filters, columns, quote)
    if not where:
        return execute_raw_sql(datasource, sql, database, params=params)

    body, order_by = _split_trailing_order_by(sql)
    wrapped = f"SELECT * FROM (\n{body}\n) AS dse_sub\nWHERE {where}"
    if order_by:
        wrapped = f"{wrapped}\n{order_by}"
    merged = {**(params or {}), **fparams}
    return execute_raw_sql(datasource, wrapped, database, params=merged)
