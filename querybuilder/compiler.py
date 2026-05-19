"""Compiles a visual query-builder spec into safe, dialect-correct SQL using
SQLAlchemy Core. The compiler only ever emits SELECT statements, and all
literal values flow through bind parameters — so it is injection-safe.
"""
from django.conf import settings
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy import column as sa_column
from sqlalchemy import table as sa_table

from connections.engine import get_engine

from .spec import validate_spec  # noqa: F401  (re-exported for callers)


class CompileError(Exception):
    """Raised when a structurally valid spec cannot be turned into SQL."""


class CompiledQuery:
    def __init__(self, statement, sql, dialect):
        self.statement = statement
        self.sql = sql
        self.dialect = dialect


def _as_list(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip() != ""]


def _aggregate(name, col, dialect_name):
    if name == "COUNT":
        return func.count(col)
    if name == "COUNT_DISTINCT":
        return func.count(col.distinct())
    if name == "SUM":
        return func.sum(col)
    if name == "AVG":
        return func.avg(col)
    if name == "MIN":
        return func.min(col)
    if name == "MAX":
        return func.max(col)
    if name == "STDDEV":
        if dialect_name == "mssql":
            return func.stdev(col)
        if dialect_name == "sqlite":
            raise CompileError("STDDEV is not supported on SQLite / file sources.")
        return func.stddev(col)
    raise CompileError(f"Unknown aggregate '{name}'.")


def _compare(expr, operator, value):
    if operator == "=":
        return expr == value
    if operator == "!=":
        return expr != value
    if operator == ">":
        return expr > value
    if operator == "<":
        return expr < value
    if operator == ">=":
        return expr >= value
    if operator == "<=":
        return expr <= value
    if operator == "LIKE":
        return expr.like(value)
    if operator == "NOT LIKE":
        return ~expr.like(value)
    if operator == "IN":
        return expr.in_(_as_list(value))
    if operator == "NOT IN":
        return ~expr.in_(_as_list(value))
    if operator == "IS NULL":
        return expr.is_(None)
    if operator == "IS NOT NULL":
        return expr.isnot(None)
    if operator == "BETWEEN":
        values = _as_list(value)
        if len(values) != 2:
            raise CompileError("BETWEEN requires exactly two values.")
        return expr.between(values[0], values[1])
    raise CompileError(f"Unsupported operator '{operator}'.")


def _build_tables(spec):
    """Build a {table_name: sqlalchemy.table} map carrying every referenced
    column, so identifiers are quoted correctly per dialect."""
    refs: dict[str, dict] = {}

    def reg(name, schema=None):
        entry = refs.setdefault(name, {"schema": schema, "columns": set()})
        if schema and not entry["schema"]:
            entry["schema"] = schema

    base = spec["base_table"]
    reg(base["name"], base["schema"])
    for j in spec["joins"]:
        reg(j["right_table"]["name"], j["right_table"]["schema"])
        reg(j["left_table"])
        refs[j["left_table"]]["columns"].add(j["left_column"])
        refs[j["right_table"]["name"]]["columns"].add(j["right_column"])
    for c in spec["columns"]:
        reg(c["table"])
        if c["column"] != "*":
            refs[c["table"]]["columns"].add(c["column"])
    for section in ("filters", "group_by", "having", "order_by"):
        for item in spec[section]:
            reg(item["table"])
            refs[item["table"]]["columns"].add(item["column"])

    return {
        name: sa_table(
            name,
            *[sa_column(c) for c in sorted(meta["columns"])],
            schema=meta["schema"],
        )
        for name, meta in refs.items()
    }


def _col(tables, table_name, column_name):
    if table_name not in tables:
        raise CompileError(f"Reference to unknown table '{table_name}'.")
    try:
        return tables[table_name].c[column_name]
    except KeyError:
        raise CompileError(f"Unknown column '{column_name}' on table '{table_name}'.")


def compile_spec(datasource, raw_spec, database=None):
    """Compile a query spec for a data source. Returns a CompiledQuery."""
    spec = validate_spec(raw_spec)
    engine = get_engine(datasource, database)
    dialect = engine.dialect
    dialect_name = dialect.name
    tables = _build_tables(spec)

    # ---- SELECT ----
    select_cols = []
    for c in spec["columns"]:
        agg = c["aggregate"]
        if c["column"] == "*":
            if agg in (None, "COUNT"):
                expr = func.count()
            else:
                raise CompileError("'*' can only be used with COUNT.")
        else:
            col = _col(tables, c["table"], c["column"])
            expr = _aggregate(agg, col, dialect_name) if agg else col
        if c["alias"]:
            expr = expr.label(c["alias"])
        elif agg:
            base_name = "all" if c["column"] == "*" else c["column"]
            expr = expr.label(f"{agg.lower()}_{base_name}")
        select_cols.append(expr)

    # ---- FROM + JOINS ----
    base_name = spec["base_table"]["name"]
    from_obj = tables[base_name]
    known = {base_name}
    for j in spec["joins"]:
        left, right = j["left_table"], j["right_table"]["name"]
        if left not in known:
            raise CompileError(
                f"Join uses table '{left}' before it is part of the query."
            )
        onclause = _col(tables, left, j["left_column"]) == _col(
            tables, right, j["right_column"]
        )
        right_tbl = tables[right]
        join_type = j["join_type"]
        if join_type == "INNER":
            from_obj = from_obj.join(right_tbl, onclause)
        elif join_type == "LEFT":
            from_obj = from_obj.join(right_tbl, onclause, isouter=True)
        elif join_type == "FULL":
            from_obj = from_obj.join(right_tbl, onclause, full=True)
        elif join_type == "RIGHT":
            # "A RIGHT JOIN B"  is equivalent to  "B LEFT JOIN A"
            from_obj = right_tbl.join(from_obj, onclause, isouter=True)
        known.add(right)

    stmt = select(*select_cols).select_from(from_obj)
    if spec["distinct"]:
        stmt = stmt.distinct()

    # ---- WHERE ----
    where_clause = None
    for f in spec["filters"]:
        expr = _compare(
            _col(tables, f["table"], f["column"]), f["operator"], f["value"]
        )
        if where_clause is None:
            where_clause = expr
        elif f["connector"] == "OR":
            where_clause = or_(where_clause, expr)
        else:
            where_clause = and_(where_clause, expr)
    if where_clause is not None:
        stmt = stmt.where(where_clause)

    # ---- GROUP BY ----
    if spec["group_by"]:
        stmt = stmt.group_by(
            *[_col(tables, g["table"], g["column"]) for g in spec["group_by"]]
        )

    # ---- HAVING ----
    having_clause = None
    for h in spec["having"]:
        agg_expr = _aggregate(
            h["aggregate"], _col(tables, h["table"], h["column"]), dialect_name
        )
        expr = _compare(agg_expr, h["operator"], h["value"])
        having_clause = expr if having_clause is None else and_(having_clause, expr)
    if having_clause is not None:
        stmt = stmt.having(having_clause)

    # ---- ORDER BY ----
    if spec["order_by"]:
        order_cols = []
        for o in spec["order_by"]:
            col = _col(tables, o["table"], o["column"])
            order_cols.append(desc(col) if o["direction"] == "DESC" else asc(col))
        stmt = stmt.order_by(*order_cols)

    # ---- LIMIT (always capped to the platform maximum) ----
    max_rows = settings.DSE_QUERY_MAX_ROWS
    stmt = stmt.limit(min(spec["limit"] or max_rows, max_rows))

    # ---- compile to a human-readable SQL string for display ----
    try:
        sql = str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    except Exception:  # noqa: BLE001 - fall back to parameterized text
        sql = str(stmt.compile(dialect=dialect))

    return CompiledQuery(stmt, sql, dialect_name)
