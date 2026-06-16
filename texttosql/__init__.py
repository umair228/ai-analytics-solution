"""Live text-to-SQL — "talk to your whole database".

Phase 1 vertical slice (see DIS_TalkToData_Plan.md §7 / §10): given a natural-
language question and a registered :class:`connections.models.DataSource`, build
a compact live-schema context (real tables/columns + data conventions), have the
configured LLM write ONE read-only SQL query, enforce read-only + a row cap, run
it through the existing :mod:`querybuilder.executor`, and return the rows plus the
exact SQL. One bounded repair retry feeds a DB error back to the model.

Deliberately minimal and safe:
  * Read-only only (``assert_read_only`` is reused verbatim).
  * Row-capped (dialect-aware TOP/LIMIT injection + the executor's fetch cap).
  * No new dependencies, no DB models/migrations.

Later phases add the curated semantic layer (Phase 3), schema-RAG retrieval, the
full validate/repair + sanity gates (Phase 4) and the cached replica (Phase 2);
this slice proves the loop end-to-end against a safe target.
"""
from .runner import run_text_to_sql

__all__ = ["run_text_to_sql"]
