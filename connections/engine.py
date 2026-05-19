"""SQLAlchemy engine factory — turns any DataSource into a connection engine.

This is the abstraction that lets one codebase talk to SQL Server, Oracle,
PostgreSQL, MySQL, SQLite and (via materialization) Excel/CSV uniformly.
"""
from django.conf import settings
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine

from .constants import DEFAULT_PORTS, SourceType

# Engines are pooled and cached per data source + target database.
_engine_cache: dict[str, Engine] = {}


class ConnectionConfigError(Exception):
    """Raised when a DataSource cannot be turned into a valid engine."""


def _parse_extra_params(raw: str) -> dict:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            if key.strip():
                out[key.strip()] = value.strip()
    return out


def build_url(datasource, database: str | None = None):
    """Build a SQLAlchemy URL (or sqlite URL string) for the data source."""
    st = datasource.source_type
    opts = datasource.options or {}
    db = database or datasource.database_name
    port = datasource.port or DEFAULT_PORTS.get(st)

    if st == SourceType.MSSQL:
        query = {"driver": opts.get("driver", "ODBC Driver 18 for SQL Server")}
        query.update(_parse_extra_params(opts.get("extra_params", "TrustServerCertificate=yes")))
        if opts.get("windows_auth"):
            query["Trusted_Connection"] = "yes"
            return URL.create(
                "mssql+pyodbc", host=datasource.host, port=port,
                database=db, query=query,
            )
        return URL.create(
            "mssql+pyodbc", username=datasource.username, password=datasource.password,
            host=datasource.host, port=port, database=db, query=query,
        )

    if st == SourceType.ORACLE:
        service = opts.get("service_name") or db
        query = {}
        if opts.get("sid"):
            query["sid"] = opts["sid"]
        elif service:
            query["service_name"] = service
        return URL.create(
            "oracle+oracledb", username=datasource.username, password=datasource.password,
            host=datasource.host, port=port or 1521, query=query,
        )

    if st == SourceType.POSTGRES:
        return URL.create(
            "postgresql+psycopg2", username=datasource.username, password=datasource.password,
            host=datasource.host, port=port, database=db,
        )

    if st == SourceType.MYSQL:
        return URL.create(
            "mysql+pymysql", username=datasource.username, password=datasource.password,
            host=datasource.host, port=port, database=db, query={"charset": "utf8mb4"},
        )

    if st == SourceType.SQLITE:
        path = opts.get("path") or db
        if not path:
            raise ConnectionConfigError("SQLite data source requires a file path.")
        return f"sqlite:///{path}"

    if st in SourceType.FILE_TYPES:
        path = opts.get("materialized_path")
        if not path:
            raise ConnectionConfigError(
                "This file data source has no uploaded file yet — upload one first."
            )
        return f"sqlite:///{path}"

    raise ConnectionConfigError(f"Unsupported source type: {st}")


def _connect_args(datasource) -> dict:
    st = datasource.source_type
    if st in (SourceType.POSTGRES, SourceType.MYSQL):
        return {"connect_timeout": settings.DSE_CONNECTION_TEST_TIMEOUT}
    if st == SourceType.MSSQL:
        return {"timeout": settings.DSE_QUERY_TIMEOUT_SECONDS}
    return {}


def get_engine(datasource, database: str | None = None) -> Engine:
    """Return a pooled SQLAlchemy engine for the data source."""
    stamp = datasource.updated_at.timestamp() if datasource.updated_at else 0
    key = f"{datasource.id}:{database or datasource.database_name}:{stamp}"
    cached = _engine_cache.get(key)
    if cached is not None:
        return cached

    # Dispose any stale engines for this data source (config changed).
    for stale in [k for k in _engine_cache if k.startswith(f"{datasource.id}:")]:
        try:
            _engine_cache.pop(stale).dispose()
        except Exception:  # noqa: BLE001
            pass

    engine = create_engine(
        build_url(datasource, database),
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=_connect_args(datasource),
        future=True,
    )
    _engine_cache[key] = engine
    return engine
