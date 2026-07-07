"""Schema-introspection service — discovers databases, schemas, tables,
columns and foreign keys for any DataSource via SQLAlchemy.
"""
import re

from sqlalchemy import column as sa_column
from sqlalchemy import inspect, select
from sqlalchemy import table as sa_table
from sqlalchemy import text

from core.serialization import jsonable_rows

from .constants import SourceType
from .engine import get_engine

_DB_LIST_SQL = {
    SourceType.MSSQL: "SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name",
    SourceType.POSTGRES: (
        "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"
    ),
    SourceType.MYSQL: "SHOW DATABASES",
}


def test_connection(datasource) -> tuple[bool, str]:
    """Open a connection and run a trivial query. Returns (ok, error)."""
    try:
        engine = get_engine(datasource)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def list_databases(datasource) -> list[str]:
    st = datasource.source_type
    if st in _DB_LIST_SQL:
        engine = get_engine(datasource)
        with engine.connect() as conn:
            return [row[0] for row in conn.execute(text(_DB_LIST_SQL[st]))]
    if st == SourceType.ORACLE:
        opts = datasource.options or {}
        return [opts.get("service_name") or datasource.database_name or "ORCL"]
    return [datasource.database_name or "main"]


def list_schemas(datasource, database: str | None = None) -> list[str]:
    engine = get_engine(datasource, database)
    try:
        return sorted(inspect(engine).get_schema_names())
    except Exception:  # noqa: BLE001
        return []


def list_tables(datasource, schema=None, database=None) -> list[dict]:
    engine = get_engine(datasource, database)
    insp = inspect(engine)
    schema = schema or datasource.schema_name or None
    out = [
        {"name": name, "type": "table", "schema": schema}
        for name in insp.get_table_names(schema=schema)
    ]
    try:
        out += [
            {"name": name, "type": "view", "schema": schema}
            for name in insp.get_view_names(schema=schema)
        ]
    except Exception:  # noqa: BLE001
        pass
    return sorted(out, key=lambda item: item["name"].lower())


def list_columns(datasource, table, schema=None, database=None) -> list[dict]:
    engine = get_engine(datasource, database)
    insp = inspect(engine)
    schema = schema or datasource.schema_name or None
    columns = insp.get_columns(table, schema=schema)
    try:
        pk = set(insp.get_pk_constraint(table, schema=schema).get("constrained_columns") or [])
    except Exception:  # noqa: BLE001
        pk = set()
    return [
        {
            "name": col["name"],
            "type": str(col.get("type", "")),
            "nullable": bool(col.get("nullable", True)),
            "primary_key": col["name"] in pk,
        }
        for col in columns
    ]


def get_foreign_keys(datasource, table, schema=None, database=None) -> list[dict]:
    engine = get_engine(datasource, database)
    insp = inspect(engine)
    schema = schema or datasource.schema_name or None
    try:
        fks = insp.get_foreign_keys(table, schema=schema)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "constrained_columns": fk.get("constrained_columns", []),
            "referred_schema": fk.get("referred_schema"),
            "referred_table": fk.get("referred_table"),
            "referred_columns": fk.get("referred_columns", []),
        }
        for fk in fks
    ]


def preview_table(datasource, table, schema=None, database=None, limit=100) -> dict:
    engine = get_engine(datasource, database)
    insp = inspect(engine)
    schema = schema or datasource.schema_name or None
    columns = [c["name"] for c in insp.get_columns(table, schema=schema)]
    sa = sa_table(table, *[sa_column(c) for c in columns], schema=schema)
    stmt = select(sa).limit(limit)
    with engine.connect() as conn:
        rows = jsonable_rows(conn.execute(stmt))
    return {"columns": columns, "rows": rows, "row_count": len(rows)}


# ──────────────────────────────────────────────────────────────────────────
#  Filter discovery — classify columns into dashboard filter controls so the
#  system can "go through the database and set up the filters" automatically.
# ──────────────────────────────────────────────────────────────────────────

FILTER_DATE = "date-range"      # date / datetime  → from–to date picker
FILTER_CATEGORY = "dropdown"    # low-cardinality  → select of distinct values
FILTER_TEXT = "text-search"     # free text        → contains search
FILTER_NUMBER = "number-range"  # numeric          → min–max range

_DATE_TYPE_HINTS = ("date", "time", "timestamp", "datetimeoffset", "smalldatetime")
_NUM_TYPE_HINTS = ("int", "decimal", "numeric", "float", "real", "double",
                   "money", "number", "bigint", "smallint", "tinyint")
_TEXT_TYPE_HINTS = ("char", "text", "clob", "string", "uuid", "uniqueidentifier")
# Column-name hints — catch dates stored as strings, and obvious categories.
_DATE_NAME_RE = re.compile(r"(_date$|^date$|_on$|_dt$|date_|_time$|timestamp)", re.I)
_CATEGORY_NAME_RE = re.compile(
    r"(status|type|category|grade|state|gender|relationship|product|project|"
    r"priority|stage|class|group|lab|department|method|condition|result)", re.I)
# System / audit / plumbing columns — valid but rarely what a user filters on,
# so we de-prioritise them and let the UI collapse them by default.
_SYSTEM_NAME_RE = re.compile(
    r"(^c_|^t_|_guid$|guid|_id$|^id$|audit|changed|_by$|_rev|"
    r"_seq|trans_num|version|disp_flds|owner|template|object_class)", re.I)
# Columns that almost always make good filters in a LIMS / lab context.
_RECOMMENDED_NAME_RE = re.compile(
    r"(login_date|recd_date|sampled_date|date_received|date_completed|"
    r"due_date|status|sample_type|product|project|^lab$|priority|"
    r"customer|location|result|analysis|stage)", re.I)


def classify_filter_type(sql_type: str, name: str) -> str:
    """Infer the best filter control for a column from its SQL type + name."""
    t = (sql_type or "").lower()
    n = (name or "").lower()
    if any(h in t for h in _DATE_TYPE_HINTS):
        return FILTER_DATE
    if _DATE_NAME_RE.search(n) and not any(h in t for h in _TEXT_TYPE_HINTS):
        return FILTER_DATE
    if any(h in t for h in _NUM_TYPE_HINTS) and not any(h in t for h in _TEXT_TYPE_HINTS):
        return FILTER_NUMBER
    if _CATEGORY_NAME_RE.search(n):
        return FILTER_CATEGORY
    return FILTER_TEXT


def _quote(engine, ident: str) -> str:
    """Dialect-correct identifier quoting (handles [SQL Server], \"pg\", `mysql`)."""
    try:
        return engine.dialect.identifier_preparer.quote(ident)
    except Exception:  # noqa: BLE001
        return f'"{ident}"'


def classify_columns(datasource, table, schema=None, database=None) -> list[dict]:
    """Return every column of a table tagged with a suggested filter control.

    Lightweight: one metadata round-trip, no per-column data scans. Distinct
    values / min-max are fetched lazily via ``column_profile``.
    """
    cols = list_columns(datasource, table, schema=schema, database=database)
    out = []
    for c in cols:
        name = c["name"]
        ftype = classify_filter_type(c.get("type", ""), name)
        is_system = bool(_SYSTEM_NAME_RE.search(name))
        out.append({
            "column": name,
            "table": table,
            "data_type": c.get("type", ""),
            "filter_type": ftype,
            "nullable": c.get("nullable", True),
            "primary_key": c.get("primary_key", False),
            "system": is_system,
            "recommended": bool(_RECOMMENDED_NAME_RE.search(name)) and not is_system,
        })
    # Useful ordering: recommended first, system last; dates before categories.
    rank = {FILTER_DATE: 0, FILTER_CATEGORY: 1, FILTER_NUMBER: 2, FILTER_TEXT: 3}
    out.sort(key=lambda x: (
        not x["recommended"], x["system"],
        rank.get(x["filter_type"], 9), x["column"].lower(),
    ))
    return out


def column_profile(datasource, table, column, schema=None, database=None,
                   max_distinct=100) -> dict:
    """Enrich one column on demand: distinct values (categories) or min/max
    (dates / numbers). Used to populate filter controls with real values."""
    engine = get_engine(datasource, database)
    schema = schema or datasource.schema_name or None
    qtable = (f"{_quote(engine, schema)}.{_quote(engine, table)}"
              if schema else _quote(engine, table))
    qcol = _quote(engine, column)

    # Determine type from metadata so we know what to fetch.
    meta = {c["name"]: c.get("type", "") for c in
            list_columns(datasource, table, schema=schema, database=database)}
    ftype = classify_filter_type(meta.get(column, ""), column)

    result = {"column": column, "table": table, "filter_type": ftype}
    with engine.connect() as conn:
        if ftype in (FILTER_DATE, FILTER_NUMBER):
            row = conn.execute(text(
                f"SELECT MIN({qcol}) AS lo, MAX({qcol}) AS hi FROM {qtable}"
            )).fetchone()
            lo, hi = (row[0], row[1]) if row else (None, None)
            result["min"] = jsonable_rows([(lo,)])[0][0] if lo is not None else None
            result["max"] = jsonable_rows([(hi,)])[0][0] if hi is not None else None
        else:
            ndistinct = conn.execute(text(
                f"SELECT COUNT(DISTINCT {qcol}) FROM {qtable}"
            )).scalar() or 0
            result["distinct_count"] = int(ndistinct)
            if ndistinct <= max_distinct:
                result["filter_type"] = FILTER_CATEGORY
                rows = conn.execute(text(
                    f"SELECT DISTINCT {qcol} AS v FROM {qtable} "
                    f"WHERE {qcol} IS NOT NULL ORDER BY v"
                )).fetchmany(max_distinct)
                result["values"] = [r[0] for r in jsonable_rows(rows)]
            else:
                result["filter_type"] = FILTER_TEXT
    return result


_FROM_JOIN_RE = re.compile(
    r"\b(?:from|join)\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)", re.I)
_SQL_KEYWORDS = {"select", "where", "group", "order", "on", "and", "or",
                 "inner", "left", "right", "outer", "cross", "full"}


def source_tables_from_sql(sql: str) -> list[dict]:
    """Best-effort parse of FROM/JOIN targets in a SELECT (for filter discovery).

    Returns [{"schema": str|None, "table": str}] de-duplicated, order-preserved.
    """
    out, seen = [], set()
    for raw in _FROM_JOIN_RE.findall(sql or ""):
        ref = raw.strip().strip("[]\"`")
        if "." in ref:
            schema, table = ref.split(".", 1)
            schema = schema.strip("[]\"`") or None
        else:
            schema, table = None, ref
        table = table.strip("[]\"`")
        if not table or table.lower() in _SQL_KEYWORDS:
            continue
        key = (schema, table.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"schema": schema, "table": table})
    return out
