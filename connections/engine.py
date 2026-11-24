"""SQLAlchemy engine factory — turns any DataSource into a connection engine.

This is the abstraction that lets one codebase talk to SQL Server, Oracle,
PostgreSQL, MySQL, SQLite and (via materialization) Excel/CSV uniformly.
"""
import os

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


def _snowflake_path(database, schema) -> str:
    """Snowflake carries database/schema in the URL PATH, not as query params."""
    if database and schema:
        return f"{database}/{schema}"
    return database or ""


# The lakehouse dialects are optional extras: heavy dependencies that most
# deployments (an on-prem LabWare LIMS) will never install. SQLAlchemy's own
# "Can't load plugin" error is unhelpful, so translate it into the one sentence
# that actually fixes the problem.
_OPTIONAL_DRIVERS = {
    SourceType.DATABRICKS: (
        "databricks-sqlalchemy",
        "Databricks support is an optional extra. Install it on the server: "
        "pip install 'databricks-sqlalchemy'",
    ),
    SourceType.SNOWFLAKE: (
        "snowflake-sqlalchemy",
        "Snowflake support is an optional extra. Install it on the server: "
        "pip install 'snowflake-sqlalchemy'",
    ),
}


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
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                raise ConnectionConfigError(
                    f"SQLite file not found and parent directory does not exist: {path}"
                )
            raise ConnectionConfigError(
                f"SQLite database file does not exist: {path}\n"
                "Check the path in the connection settings — it must be an absolute path "
                "accessible inside the server/container."
            )
        return f"sqlite:///{path}"

    if st == SourceType.DATABRICKS:
        # databricks://token:<PAT>@<host>?http_path=...&catalog=...&schema=...
        # The username is the literal string "token" — that is the connector's
        # convention, not a placeholder we forgot to fill in.
        http_path = opts.get("http_path")
        if not http_path:
            raise ConnectionConfigError(
                "Databricks requires the SQL warehouse HTTP path "
                "(e.g. /sql/1.0/warehouses/abc123). Find it under "
                "SQL Warehouses → your warehouse → Connection details."
            )
        if not datasource.password:
            raise ConnectionConfigError(
                "Databricks requires a personal access token (or service-principal "
                "token). DIS only ever reads — scope it to SELECT."
            )
        query = {"http_path": http_path}
        if db:
            query["catalog"] = db
        if datasource.schema_name:
            query["schema"] = datasource.schema_name
        return URL.create(
            "databricks", username="token", password=datasource.password,
            host=datasource.host, port=port or 443, query=query,
        )

    if st == SourceType.SNOWFLAKE:
        # snowflake://<user>:<pw>@<account>/<database>/<schema>?warehouse=..&role=..
        # NB `host` here is the ACCOUNT IDENTIFIER (orgname-account), not a hostname.
        if not datasource.host:
            raise ConnectionConfigError(
                "Snowflake requires the account identifier (e.g. myorg-prod1), "
                "not a hostname."
            )
        query = {}
        if opts.get("warehouse"):
            query["warehouse"] = opts["warehouse"]
        if opts.get("role"):
            query["role"] = opts["role"]
        return URL.create(
            "snowflake", username=datasource.username, password=datasource.password,
            host=datasource.host, database=_snowflake_path(db, datasource.schema_name),
            query=query,
        )

    if st == SourceType.ODBC:
        dsn = opts.get("dsn") or datasource.database_name
        if not dsn:
            raise ConnectionConfigError("ODBC data source requires a DSN name.")
        parts = [f"DSN={dsn}"]
        if opts.get("windows_auth"):
            parts.append("Trusted_Connection=yes")
        elif datasource.username:
            parts.append(f"UID={datasource.username}")
            parts.append(f"PWD={datasource.password}")
        if opts.get("extra_params"):
            parts.append(opts["extra_params"])
        odbc_str = ";".join(parts)
        return URL.create("mssql+pyodbc", query={"odbc_connect": odbc_str})

    if st in SourceType.FILE_TYPES:
        path = opts.get("materialized_path")
        if not path:
            raise ConnectionConfigError(
                "This file data source has no uploaded file yet — upload one first."
            )
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise ConnectionConfigError(
                f"Materialized file not found: {path}\n"
                "Re-upload the file to refresh this data source."
            )
        return f"sqlite:///{path}"

    raise ConnectionConfigError(f"Unsupported source type: {st}")


def _connect_args(datasource) -> dict:
    st = datasource.source_type
    if st in (SourceType.POSTGRES, SourceType.MYSQL):
        return {"connect_timeout": settings.DSE_CONNECTION_TEST_TIMEOUT}
    if st in (SourceType.MSSQL, SourceType.ODBC):
        return {"timeout": settings.DSE_QUERY_TIMEOUT_SECONDS}
    # The lakehouse connectors speak HTTPS and manage their own timeouts; passing a
    # DBAPI `timeout` kwarg to them is a TypeError, not a setting.
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

    try:
        engine = create_engine(
            build_url(datasource, database),
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=_connect_args(datasource),
            future=True,
        )
    except Exception as exc:  # noqa: BLE001
        hint = _OPTIONAL_DRIVERS.get(datasource.source_type)
        if hint and ("plugin" in str(exc).lower() or "module" in str(exc).lower()):
            raise ConnectionConfigError(f"{hint[1]}\n\n(underlying error: {exc})") from exc
        raise
    _engine_cache[key] = engine
    return engine
