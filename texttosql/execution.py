"""Statement-level deadline for generated SQL.

The MSSQL/ODBC ``timeout`` in connections.engine is connection-*acquisition* only,
not a query runtime cap — a pathological generated query could otherwise run
unbounded on the database (DIS_TalkToData_Plan.md §9). This runs the execution in
a worker thread, bounds the caller's wait, and on overrun disposes the datasource's
engine so its pooled connections (and the in-flight server query) are dropped.

Honest limitation: thread-join + engine-dispose reliably unblocks the *app* and
abandons the connection; truly cancelling the server-side query mid-flight is
driver-specific (pyodbc ``Connection.cancel()`` / sqlite ``interrupt()``) and is a
later refinement. Disposing the pool is the portable best-effort today.
"""
from __future__ import annotations

import threading

from django.conf import settings

from connections.engine import get_engine
from querybuilder import executor


def execute_with_deadline(datasource, sql, database=None, seconds=None):
    """Run ``executor.execute_raw_sql`` under a wall-clock deadline.

    Returns the result payload, or raises ``QueryError`` on timeout/failure.
    """
    seconds = int(seconds or getattr(settings, "DSE_QUERY_TIMEOUT_SECONDS", 30))
    box: dict = {}

    def _work():
        try:
            box["payload"] = executor.execute_raw_sql(datasource, sql, database=database)
        except Exception as exc:  # noqa: BLE001 - re-raised on the calling thread
            box["error"] = exc

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(seconds)

    if worker.is_alive():
        # Drop the pool so the abandoned server query is released; the daemon
        # thread dies with the connection.
        try:
            get_engine(datasource).dispose()
        except Exception:  # noqa: BLE001
            pass
        raise executor.QueryError(f"statement deadline exceeded ({seconds}s)")

    if "error" in box:
        raise box["error"]
    return box["payload"]
