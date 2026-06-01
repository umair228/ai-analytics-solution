"""
Connection helpers for the LIMS forecasting source database (SQL Server).

The forecasting views read operational tables directly over pyodbc rather than
through the Django ORM (matching the original Portal-BE implementation), so the
LIMS DB stays decoupled from the app-metadata DB. Configuration comes from
``settings.FORECAST_DB`` / ``settings.FORECAST_INVENTORY_DB`` (see ``.env``).
"""
import pyodbc
import pandas as pd
from django.conf import settings


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


def get_connection(autocommit: bool = False):
    """Open a pyodbc connection to the main LIMS forecast DB."""
    return pyodbc.connect(
        _conn_str(settings.FORECAST_DB),
        autocommit=autocommit,
        timeout=getattr(settings, "FORECAST_DB_TIMEOUT", 15),
    )


def get_inventory_connection(autocommit: bool = True):
    """Open a pyodbc connection to the inventory LIMS DB (may equal the main DB)."""
    return pyodbc.connect(
        _conn_str(settings.FORECAST_INVENTORY_DB),
        autocommit=autocommit,
        timeout=getattr(settings, "FORECAST_DB_TIMEOUT", 15),
    )


def read_sql(query: str, conn, params=None, parse_dates=None) -> pd.DataFrame:
    """
    Run a query on a raw pyodbc connection and return a DataFrame.

    Implemented with cursor.fetchall() instead of ``pandas.read_sql`` so it is
    robust across pandas versions (pandas >= 2 warns/errors on non-SQLAlchemy
    connections) and needs no SQLAlchemy dependency.
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
