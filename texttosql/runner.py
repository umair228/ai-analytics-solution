"""Orchestrate one trustworthy text-to-SQL answer (Plan §8):

  generate → read-only + name-allow-list + group-by sanity → row-cap → execute
  (under a statement deadline) → result sanity gates → on ANY violation, feed the
  exact reason back and retry (bounded) → still failing: return cannot_answer with
  the available tables (never guess).

Datasource permission/ownership is enforced by the caller (ai.tools); this module
is pure mechanism and takes an already-resolved DataSource.
"""
from __future__ import annotations

from django.conf import settings

from querybuilder.executor import QueryError, assert_read_only

from .execution import execute_with_deadline
from .generator import generate_sql
from .schema import build_schema
from .sql_tools import extract_sql, inject_row_limit
from .validator import sanity_violations, validate_sql


def run_text_to_sql(datasource, question, *, database=None, max_rows=None,
                    max_attempts=None) -> dict:
    """Answer ``question`` against ``datasource`` by generating + running SQL.

    Success → {"understood": True, "sql", "columns", "rows", "row_count",
               "truncated", "attempts", "freshness", "warnings", "datasource"}.
    Give-up → {"understood": False, "error", "available_tables", "attempts"}.
    """
    n = int(max_rows or getattr(settings, "DSE_QUERY_MAX_ROWS", 5000))
    max_attempts = int(max_attempts or getattr(settings, "DSE_TEXTTOSQL_MAX_ATTEMPTS", 3))

    from semantics.layer import get_semantic_layer
    from semantics.router import match_certified_metric

    layer = get_semantic_layer(datasource)

    # Certified-metric fast path: a high-confidence match runs trusted, curated SQL
    # deterministically (no LLM). On drift / dialect mismatch, fall through.
    if layer.is_present():
        metric = match_certified_metric(layer, question)
        if metric:
            try:
                assert_read_only(metric.sql)
                capped = inject_row_limit(metric.sql, datasource.source_type, n)
                payload = execute_with_deadline(datasource, capped, database=database)
                return {
                    "understood": True, "question": question,
                    "datasource": {"id": datasource.id, "name": datasource.name},
                    "path": f"certified_metric:{metric.key}",
                    "sql": capped, "columns": payload["columns"], "rows": payload["rows"],
                    "row_count": payload["row_count"], "truncated": payload["truncated"],
                    "attempts": 0, "freshness": "live", "warnings": [],
                }
            except QueryError:
                pass  # fall through to long-tail generation

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

        warnings = sanity_violations(capped, payload)
        if warnings and not last:
            # Repair once on a suspicious-but-valid result; accept it on the last try
            # rather than loop forever (and surface the warning to the agent).
            feedback = " ".join(warnings)
            tried.append({"sql": capped, "issue": feedback})
            continue

        return {
            "understood": True,
            "question": question,
            "datasource": {"id": datasource.id, "name": datasource.name},
            "path": "generated",
            "sql": capped,
            "columns": payload["columns"],
            "rows": payload["rows"],
            "row_count": payload["row_count"],
            "truncated": payload["truncated"],
            "attempts": attempt + 1,
            "freshness": "live",
            "warnings": warnings,
        }

    return {
        "understood": False,
        "question": question,
        "datasource": {"id": datasource.id, "name": datasource.name},
        "path": "none",
        "error": ("Could not produce a valid query after "
                  f"{max_attempts} attempts. Last issue: {feedback}"),
        "available_tables": sorted(schema.table_names()),
        "attempts": tried[-max_attempts:],
    }
