"""Orchestrate one trustworthy text-to-SQL answer (Plan §8):

  generate → read-only + name-allow-list + group-by sanity → row-cap → execute
  (under a statement deadline) → result sanity gates → on ANY violation, feed the
  exact reason back and retry (bounded) → still failing: return cannot_answer with
  the available tables (never guess).

Ahead of that sits the certified-metric fast path: a curated, QA-approved query
runs deterministically with no LLM involvement at all. Its SQL is stored *portable*
and rendered for the dialect of whichever datasource is being queried (the LabWare
MSSQL source, or the Postgres/SQLite replica) — see semantics.metric_sql.

Every answer is stamped with real freshness (replication.freshness), not the
hardcoded literal "live" this module used to claim regardless of where the data
actually came from.

Datasource permission/ownership is enforced by the caller (ai.tools); this module
is pure mechanism and takes an already-resolved DataSource.
"""
from __future__ import annotations

import logging

from django.conf import settings

from querybuilder.executor import QueryError, assert_read_only
from replication.freshness import describe as describe_freshness

from .execution import execute_with_deadline
from .generator import generate_sql
from .schema import build_schema
from .sql_tools import extract_sql, inject_row_limit
from .validator import sanity_violations, validate_sql

logger = logging.getLogger(__name__)


def _answer(*, datasource, question, path, sql, payload, attempts, warnings, freshness) -> dict:
    """One successful answer, with freshness stamped on every path."""
    return {
        "understood": True,
        "question": question,
        "datasource": {"id": datasource.id, "name": datasource.name},
        "path": path,
        "sql": sql,
        "columns": payload["columns"],
        "rows": payload["rows"],
        "row_count": payload["row_count"],
        "truncated": payload["truncated"],
        "attempts": attempts,
        "warnings": list(warnings) + freshness.warnings(),
        **freshness.as_provenance(),
    }


def run_text_to_sql(datasource, question, *, database=None, max_rows=None,
                    max_attempts=None) -> dict:
    """Answer ``question`` against ``datasource`` by generating + running SQL.

    Success → {"understood": True, "sql", "columns", "rows", "row_count",
               "truncated", "attempts", "freshness", "data_as_of", "warnings",
               "datasource"}.
    Give-up → {"understood": False, "error", "available_tables", "attempts"}.
    """
    n = int(max_rows or getattr(settings, "DSE_QUERY_MAX_ROWS", 5000))
    max_attempts = int(max_attempts or getattr(settings, "DSE_TEXTTOSQL_MAX_ATTEMPTS", 3))

    from semantics.layer import get_semantic_layer, render_for
    from semantics.router import match_certified_metric

    freshness = describe_freshness(datasource)
    layer = get_semantic_layer(datasource)
    warnings: list[str] = []

    # Certified-metric fast path: a high-confidence match runs trusted, curated SQL
    # deterministically (no LLM). Portable SQL is rendered for this datasource's
    # dialect, so the same metric serves the LIMS source and the replica alike.
    if layer.is_present():
        metric = match_certified_metric(layer, question)
        if metric:
            try:
                sql = render_for(layer, metric.sql, datasource)
                assert_read_only(sql)
                cap = min(metric.row_limit or n, n)
                capped = inject_row_limit(sql, datasource.source_type, cap)
                payload = execute_with_deadline(datasource, capped, database=database)
                return _answer(
                    datasource=datasource, question=question,
                    path=f"certified_metric:{metric.key}", sql=capped, payload=payload,
                    attempts=0, warnings=[], freshness=freshness,
                )
            except QueryError as exc:
                # Fall through to generation — but LOUDLY. A silent `pass` here is
                # exactly how the metrics-vs-replica dialect incompatibility hid for
                # so long: every certified metric was failing against a replica and
                # the only symptom was that the fast path "never matched".
                logger.warning(
                    "Certified metric '%s' failed on datasource '%s' (%s) — falling back "
                    "to generated SQL. This should not happen; the metric is broken for "
                    "this dialect. Error: %s",
                    metric.key, datasource.name, datasource.source_type, exc,
                )
                warnings.append(
                    f"The certified metric '{metric.key}' failed to run and was bypassed; "
                    f"this answer was generated instead."
                )

    schema = build_schema(datasource, database=database, question=question, layer=layer)
    schema_text = schema.render()

    tried = []
    feedback = None
    prior_sql = None

    for attempt in range(max_attempts):
        last = attempt == max_attempts - 1

        raw = generate_sql(question, schema_text, datasource.source_type,
                           error_feedback=feedback, prior_sql=prior_sql)
        sql = extract_sql(raw)
        prior_sql = sql
        if not sql:
            feedback = "You did not return a SQL statement. Return one read-only SELECT."
            tried.append({"sql": (raw or "")[:300], "issue": feedback})
            continue

        violations = validate_sql(sql, schema)
        if violations:
            feedback = " ".join(violations)
            tried.append({"sql": sql, "issue": feedback})
            continue

        capped = inject_row_limit(sql, datasource.source_type, n)
        try:
            payload = execute_with_deadline(datasource, capped, database=database)
        except QueryError as exc:
            feedback = f"The query failed to run: {exc}. Fix it."
            tried.append({"sql": capped, "issue": feedback})
            continue

        sanity = sanity_violations(capped, payload)
        if sanity and not last:
            # Repair once on a suspicious-but-valid result; accept it on the last try
            # rather than loop forever (and surface the warning to the agent).
            feedback = " ".join(sanity)
            tried.append({"sql": capped, "issue": feedback})
            continue

        return _answer(
            datasource=datasource, question=question, path="generated", sql=capped,
            payload=payload, attempts=attempt + 1, warnings=warnings + sanity,
            freshness=freshness,
        )

    return {
        "understood": False,
        "question": question,
        "datasource": {"id": datasource.id, "name": datasource.name},
        "path": "none",
        "error": ("Could not produce a valid query after "
                  f"{max_attempts} attempts. Last issue: {feedback}"),
        "available_tables": sorted(schema.table_names()),
        "attempts": tried[-max_attempts:],
        "warnings": warnings,
        **freshness.as_provenance(),
    }
