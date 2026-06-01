"""
Connection helpers for the LIMS forecasting source database.

Two engines are supported, selected by ``settings.FORECAST_DB["ENGINE"]``:

* ``mssql``  — the production LabWare LIMS over **pyodbc** (Portal-BE behaviour).
* ``sqlite`` — a self-contained LIMS warehouse file (the refinery demo
  warehouse, ``media/refinery_lims.sqlite3``). Lets the downtime / sample / section /
  inventory forecasts run with no external SQL Server.

The forecasting views read operational tables directly (no Django ORM), so the
LIMS DB stays decoupled from the app-metadata DB. Use the ``tbl()`` /
``date_cast()`` helpers in views so the SQL works on either engine.
Configuration comes from ``settings.FORECAST_DB`` / ``FORECAST_INVENTORY_DB``.
"""
import sqlite3

import pandas as pd
from django.conf import settings


def is_sqlite() -> bool:
    return str(settings.FORECAST_DB.get("ENGINE", "mssql")).lower() == "sqlite"


def tbl(name: str) -> str:
    """Schema-qualify a table name for the active engine (``dbo.`` on MSSQL)."""
    return name if is_sqlite() else f"dbo.{name}"


def date_cast(col: str) -> str:
    """Cast a datetime column to a date for the active engine."""
    return f"date({col})" if is_sqlite() else f"CAST({col} AS DATE)"


def _sqlite_path(cfg: dict) -> str:
    return cfg.get("PATH") or cfg.get("NAME")


class _ClosingSqlite(sqlite3.Connection):
    """A sqlite3 connection that also closes on ``with ... as conn:`` exit
    (stdlib only commits/rolls back), matching how the views use pyodbc."""

    def __exit__(self, *exc):
        super().__exit__(*exc)
        self.close()


def _conn_str(cfg: dict) -> str:
    return (
        f"DRIVER={{{cfg['DRIVER']}}};"
        f"SERVER={cfg['HOST']},{cfg['PORT']};"
        f"DATABASE={cfg['NAME']};"
        f"UID={cfg['USER']};"
        f"PWD={cfg['PASSWORD']};"
        "TrustServerCertificate=yes;"
        "Encrypt=no;"
    )


def _open(cfg: dict, autocommit: bool):
    timeout = getattr(settings, "FORECAST_DB_TIMEOUT", 15)
    if is_sqlite():
        return sqlite3.connect(
            _sqlite_path(cfg), timeout=timeout, factory=_ClosingSqlite,
            check_same_thread=False,
        )
    import pyodbc  # imported lazily so SQLite-only hosts need no ODBC driver
    return pyodbc.connect(_conn_str(cfg), autocommit=autocommit, timeout=timeout)


def get_connection(autocommit: bool = False):
    """Open a connection to the main LIMS forecast DB."""
    return _open(settings.FORECAST_DB, autocommit)


def get_inventory_connection(autocommit: bool = True):
    """Open a connection to the inventory LIMS DB (may equal the main DB)."""
    return _open(settings.FORECAST_INVENTORY_DB, autocommit)


def read_sql(query: str, conn, params=None, parse_dates=None) -> pd.DataFrame:
    """
    Run a query on a raw DB-API connection and return a DataFrame.

    Implemented with cursor.fetchall() instead of ``pandas.read_sql`` so it is
    robust across pandas versions and engines (pyodbc and sqlite3 alike) and
    needs no SQLAlchemy dependency.
    """
    cursor = conn.cursor()
    cursor.execute(query, params or [])
    columns = [col[0] for col in cursor.description]
    rows = cursor.fetchall()
    df = pd.DataFrame.from_records(
        [tuple(r) for r in rows], columns=columns
    ) if rows else pd.DataFrame(columns=columns)
    for col in parse_dates or []:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df
