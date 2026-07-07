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


def execute_spec(datasource, raw_spec, database=None, max_rows=None):
    """Compile and execute a query spec. Returns a result payload."""
    compiled = compile_spec(datasource, raw_spec, database)
    engine = get_engine(datasource, database)
    try:
        with engine.connect() as conn:
            result = conn.execute(compiled.statement)
            return _result_payload(result, compiled.sql,
                                   max_rows or settings.DSE_QUERY_MAX_ROWS)
    except Exception as exc:  # noqa: BLE001
        raise QueryError(str(exc)) from exc


def execute_raw_sql(datasource, sql, database=None, params=None, max_rows=None):
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
            return _result_payload(result, sql,
                                   max_rows or settings.DSE_QUERY_MAX_ROWS)
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


_COMPARE_OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "=": "=", "!=": "<>"}


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

        if ftype == "compare":
            op = _COMPARE_OPS.get(f.get("op"))
            v = f.get("value")
            if op and v not in (None, ""):
                clauses.append(f"{qcol} {op} :{pk}")
                params[pk] = v
        elif ftype == "date-range":
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
            negate = bool(f.get("not"))
            if len(values) == 1:
                clauses.append(f"{qcol} {'<>' if negate else '='} :{pk}")
                params[pk] = values[0]
            elif len(values) > 1:
                names = [f"{pk}_{j}" for j in range(len(values))]
                placeholders = ", ".join(f":{n}" for n in names)
                clauses.append(f"{qcol} {'NOT IN' if negate else 'IN'} ({placeholders})")
                params.update({n: v for n, v in zip(names, values)})
    return " AND ".join(clauses), params


# ──────────────────────────────────────────────────────────────────────────
#  Full-data exploration — count / extent / aggregate / row pages computed by
#  the database over the COMPLETE result set of a dataset's query, so charts
#  and totals are never limited by the cached-preview row cap.
# ──────────────────────────────────────────────────────────────────────────

_AGG_SQL = {
    "count": "COUNT(*)",
    "count_distinct": "COUNT(DISTINCT {y})",
    "sum": "SUM({y})",
    "avg": "AVG({y})",
    "min": "MIN({y})",
    "max": "MAX({y})",
}
EXPLORE_MAX_GROUPS = 5000
EXPLORE_MAX_PAGE = 10000

# Dialect-specific date truncation for time-series bucketing. Expressions
# yield sortable ISO prefixes (YYYY / YYYY-MM / YYYY-MM-DD).
_BUCKET_SQL = {
    "mssql": {
        "day": "CONVERT(varchar(10), {x}, 23)",
        "month": "CONVERT(varchar(7), {x}, 23)",
        "year": "CONVERT(varchar(4), {x}, 23)",
    },
    "sqlite": {
        "day": "strftime('%Y-%m-%d', {x})",
        "month": "strftime('%Y-%m', {x})",
        "year": "strftime('%Y', {x})",
    },
    "postgresql": {
        "day": "to_char({x}, 'YYYY-MM-DD')",
        "month": "to_char({x}, 'YYYY-MM')",
        "year": "to_char({x}, 'YYYY')",
    },
}


def _bucket_expr(dialect, x_sql, bucket):
    """Date-truncation SQL for the dialect, or None when unsupported."""
    tmpl = _BUCKET_SQL.get(dialect, {}).get(bucket)
    return tmpl.format(x=x_sql) if tmpl else None


def execute_dataset_query(datasource, sql, database=None, params=None,
                          filters=None, columns=None, spec=None):
    """Run an exploration query over the full result of ``sql``.

    ``spec`` selects the shape:
      {"mode": "count"}                              → {"total_row_count": N}
      {"mode": "extent", "column": C}                → {"column", "min", "max"}
      {"mode": "aggregate", "x": C, "y": C, "agg": A[, "group_by": C][, "limit": N]}
                                                     → {"data": [{label, value[, group]}], ...}
      {"mode": "rows"[, "limit": N][, "offset": N]}  → row page + total_row_count

    Filters become bound WHERE predicates on a derived table; every identifier
    is checked against ``columns`` (the dataset's output allow-list) and quoted.
    """
    spec = spec or {}
    mode = spec.get("mode") or "rows"
    assert_read_only(sql)
    engine = get_engine(datasource, database)
    quote = engine.dialect.identifier_preparer.quote
    allowed = set(columns or [])
    dialect = engine.dialect.name  # 'mssql' | 'sqlite' | 'postgresql' | ...

    where, fparams = build_filter_where(filters, columns, quote)
    body, order_by = _split_trailing_order_by(sql)
    sub = f"(\n{body}\n) AS dse_sub"
    where_sql = f"\nWHERE {where}" if where else ""
    merged = {**(params or {}), **fparams}

    def safe_col(name, what):
        if not name or name not in allowed:
            raise QueryError(f"Unknown column for {what}: {name!r}")
        return quote(name)

    def run(q, bind):
        try:
            with engine.connect() as conn:
                result = conn.execute(text(q), bind)
                return list(result.keys()), result.fetchall()
        except QueryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QueryError(str(exc)) from exc

    def limited(q, limit, offset=0):
        """Dialect-correct row limiting appended to a finished query."""
        if dialect == "mssql":
            if "ORDER BY" not in q.upper().rsplit(")", 1)[-1]:
                q += "\nORDER BY (SELECT NULL)"
            return q + "\nOFFSET :dse_off ROWS FETCH NEXT :dse_lim ROWS ONLY"
        return q + "\nLIMIT :dse_lim OFFSET :dse_off"

    if mode == "count":
        _, rows = run(f"SELECT COUNT(*) FROM {sub}{where_sql}", merged)
        return {"total_row_count": int(rows[0][0] or 0)}

    if mode == "extent":
        c = safe_col(spec.get("column"), "extent")
        _, rows = run(f"SELECT MIN({c}), MAX({c}) FROM {sub}{where_sql}", merged)
        lo, hi = jsonable_rows([tuple(rows[0])])[0] if rows else (None, None)
        return {"column": spec.get("column"), "min": lo, "max": hi}

    if mode == "scalar":
        agg = (spec.get("agg") or "count").lower()
        if agg not in _AGG_SQL:
            raise QueryError(f"Unsupported aggregation: {agg!r}")
        y = safe_col(spec.get("y"), "y") if agg != "count" else ""
        val = _AGG_SQL[agg].format(y=y)
        _, rows = run(f"SELECT {val} FROM {sub}{where_sql}", merged)
        value = jsonable_rows([tuple(rows[0])])[0][0] if rows else None
        return {"value": value, "agg": agg}

    if mode == "aggregate":
        agg = (spec.get("agg") or "count").lower()
        if agg not in _AGG_SQL:
            raise QueryError(f"Unsupported aggregation: {agg!r}")
        x = safe_col(spec.get("x"), "x")
        y = safe_col(spec.get("y"), "y") if agg != "count" else ""
        val = _AGG_SQL[agg].format(y=y)
        # Optional time-series bucketing: group a date column by day/month/year
        # (chronological order) instead of by raw timestamp.
        bucket = (spec.get("x_bucket") or "").lower() or None
        if bucket:
            if bucket not in ("day", "month", "year"):
                raise QueryError(f"Unsupported x_bucket: {bucket!r}")
            x = _bucket_expr(dialect, x, bucket) or x
        limit = max(1, min(int(spec.get("limit") or 500), EXPLORE_MAX_GROUPS))
        group_by = spec.get("group_by")
        order = "dse_label ASC" if bucket else "dse_value DESC"
        if group_by:
            g = safe_col(group_by, "group_by")
            q = (f"SELECT {x} AS dse_label, {g} AS dse_group, {val} AS dse_value "
                 f"FROM {sub}{where_sql}"
                 f"\nGROUP BY {x}, {g}\nORDER BY {order}")
        else:
            q = (f"SELECT {x} AS dse_label, {val} AS dse_value FROM {sub}{where_sql}"
                 f"\nGROUP BY {x}\nORDER BY {order}")
        cols, rows = run(limited(q, limit),
                         {**merged, "dse_lim": limit + 1, "dse_off": 0})
        rows = jsonable_rows(rows)
        truncated = len(rows) > limit
        rows = rows[:limit]
        if group_by:
            data = [{"label": r[0], "group": r[1], "value": r[2]} for r in rows]
        else:
            data = [{"label": r[0], "value": r[1]} for r in rows]
        return {"data": data, "row_count": len(data), "truncated": truncated,
                "x_bucket": bucket}

    if mode == "rows":
        limit = max(1, min(int(spec.get("limit") or 1000), EXPLORE_MAX_PAGE))
        offset = max(0, int(spec.get("offset") or 0))
        q = f"SELECT * FROM {sub}{where_sql}"
        if order_by:
            q += f"\n{order_by}"
        cols, rows = run(limited(q, limit, offset),
                         {**merged, "dse_lim": limit, "dse_off": offset})
        _, count_rows = run(f"SELECT COUNT(*) FROM {sub}{where_sql}", merged)
        return {
            "columns": [str(c) for c in cols],
            "rows": jsonable_rows(rows),
            "row_count": len(rows),
            "offset": offset,
            "limit": limit,
            "total_row_count": int(count_rows[0][0] or 0),
        }

    raise QueryError(f"Unknown explore mode: {mode!r}")


def execute_raw_sql_filtered(datasource, sql, database=None, params=None,
                             filters=None, columns=None, max_rows=None):
    """Run raw SQL with optional dashboard filters applied server-side.

    ``params``  — query bind-params (e.g. pre-aggregation date params).
    ``filters`` — output-column filters, applied by wrapping the query in a
                  derived table:  SELECT * FROM (<sql>) dse_sub WHERE … ORDER BY …
    ``columns`` — the query's known output columns (the allow-list).
    """
    if not filters:
        return execute_raw_sql(datasource, sql, database, params=params,
                               max_rows=max_rows)

    assert_read_only(sql)
    engine = get_engine(datasource, database)
    quote = engine.dialect.identifier_preparer.quote
    where, fparams = build_filter_where(filters, columns, quote)
    if not where:
        return execute_raw_sql(datasource, sql, database, params=params,
                               max_rows=max_rows)

    body, order_by = _split_trailing_order_by(sql)
    wrapped = f"SELECT * FROM (\n{body}\n) AS dse_sub\nWHERE {where}"
    if order_by:
        wrapped = f"{wrapped}\n{order_by}"
    merged = {**(params or {}), **fparams}
    return execute_raw_sql(datasource, wrapped, database, params=merged,
                           max_rows=max_rows)
