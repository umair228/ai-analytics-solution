"""
Connection helper for the AI Document Search NL->SQL path.

Reads ``settings.DOCSEARCH_DB`` — a standalone connection config that defaults to
the production LabWare LIMS over SQL Server (SMJMUN_DEV). It is INTENTIONALLY
separate from ``FORECAST_DB`` so the forecasting feature can keep running against
the self-contained SQLite demo warehouse (``DB_ENGINE=sqlite``) while document
search queries the live MSSQL database (``DOCSEARCH_DB_ENGINE=mssql``).

Mirrors ``forecasting/db.py`` so the two share the same engine-portable idioms.
"""
import sqlite3

import pandas as pd
from django.conf import settings


def _cfg() -> dict:
    return settings.DOCSEARCH_DB


def is_sqlite() -> bool:
    return str(_cfg().get("ENGINE", "mssql")).lower() == "sqlite"


def tbl(name: str) -> str:
    """Schema-qualify a table name for the active engine (``dbo.`` on MSSQL)."""
    return name if is_sqlite() else f"dbo.{name}"


class _ClosingSqlite(sqlite3.Connection):
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


def get_connection(autocommit: bool = True):
    cfg = _cfg()
    timeout = getattr(settings, "DOCSEARCH_DB_TIMEOUT", 20)
    if is_sqlite():
        return sqlite3.connect(
            cfg.get("PATH") or cfg.get("NAME"), timeout=timeout,
            factory=_ClosingSqlite, check_same_thread=False,
        )
    import pyodbc  # lazy: SQLite-only hosts need no ODBC driver
    return pyodbc.connect(_conn_str(cfg), autocommit=autocommit, timeout=timeout)


def read_sql(query: str, conn, params=None) -> pd.DataFrame:
    """Run a query on a raw DB-API connection and return a DataFrame.

    Uses cursor.fetchall() rather than ``pandas.read_sql`` so it needs no
    SQLAlchemy and works on both pyodbc and sqlite3.
    """
    cursor = conn.cursor()
    cursor.execute(query, params or [])
    columns = [c[0] for c in cursor.description]
    rows = cursor.fetchall()
    return (
        pd.DataFrame.from_records([tuple(r) for r in rows], columns=columns)
        if rows else pd.DataFrame(columns=columns)
    )
