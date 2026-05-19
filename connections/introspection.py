"""Schema-introspection service — discovers databases, schemas, tables,
columns and foreign keys for any DataSource via SQLAlchemy.
"""
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
