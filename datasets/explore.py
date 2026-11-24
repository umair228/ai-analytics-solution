"""Full-data exploration of a dataset (counts, extents, aggregates, row pages), with
a snapshot cache (P10).

The board widgets call this on every load. When the dataset's source is a restored
snapshot (``options["restore_id"]``, set by ``promote_source``), its data cannot
change until the next promotion, so a result is cached under a key that includes the
restore id: the first viewer after a promotion (or ``warm_boards``) pays for the
query, everyone after reads the cache, and the next promotion changes the key. A
live source is never cached here. The cache is Django's default cache; set
``DIS_CACHE=db`` so every API worker (and the nightly warm-up) shares it.
"""
from __future__ import annotations

import hashlib
import json

from django.core.cache import cache

SNAPSHOT_TTL = 26 * 3600      # a promotion changes the key long before this matters

SPEC_KEYS = ("mode", "x", "y", "agg", "group_by", "x_bucket", "column", "limit",
             "offset", "with_count")


class ExploreUnsupported(Exception):
    """The dataset cannot be explored server-side (not a raw-SQL dataset)."""


def _snapshot_key(query, restore_id, params, filters, spec, columns) -> str:
    blob = json.dumps({
        "ds": query.datasource_id, "restore": restore_id, "q": query.pk,
        "q_at": query.updated_at, "db": query.database or "",
        "sql": hashlib.sha256((query.raw_sql or "").encode()).hexdigest(),
        "params": params, "filters": filters, "spec": spec, "columns": columns,
    }, sort_keys=True, default=str)
    return "explore:" + hashlib.sha256(blob.encode()).hexdigest()


def run_explore(dataset, body: dict, *, use_cache: bool = True) -> dict:
    """Run one exploration request for ``dataset`` (access already checked).

    Returns the executor's result with ``source`` ("database" or "cache") and
    ``skipped_filters`` when filters named columns the dataset does not have.
    Raises ``QueryError`` or ``ExploreUnsupported``.
    """
    from querybuilder.executor import QueryError, execute_dataset_query, is_unknown_column_error
    from querybuilder.models import QueryDefinition

    from .services import refresh_dataset

    q = dataset.query
    if q is None or q.mode != QueryDefinition.Mode.RAW:
        raise ExploreUnsupported("Full-data explore requires a raw-SQL dataset; "
                                 "the client falls back to the cached preview.")

    # The output-column allow-list comes from the cached result; populate it on
    # first use so a never-refreshed dataset still works.
    if not dataset.cached_columns:
        refresh_dataset(dataset)

    body = body or {}
    spec = {k: body.get(k) for k in SPEC_KEYS}
    merged_params = {**q.declared_param_defaults(),
                     **(dataset.param_defaults or {}),
                     **(body.get("params") or {})}
    # Only well-formed entries — a malformed filter must degrade, not 500.
    filters = [f for f in (body.get("filters") or [])
               if isinstance(f, dict) and f.get("column")]

    def run(flts):
        return execute_dataset_query(
            q.datasource, q.raw_sql, q.database or None,
            params=merged_params or None,
            filters=flts,
            columns=dataset.cached_columns or [],
            spec=spec,
        )

    def split(flts, cols):
        kept = [f for f in flts if f["column"] in cols]
        skip = sorted({str(f["column"]) for f in flts if f["column"] not in cols})
        return kept, skip

    columns = dataset.cached_columns or []
    kept, skipped = split(filters, columns)

    # Filters were dropped but the query changed since the last refresh — the cache
    # may simply be stale: heal proactively and re-split (covers a NEW output column
    # the stale cache didn't know about).
    if skipped and dataset.last_refreshed_at and q.updated_at \
            and q.updated_at > dataset.last_refreshed_at:
        try:
            refresh_dataset(dataset)
            columns = dataset.cached_columns or []
            kept, skipped = split(filters, columns)
        except Exception:  # noqa: BLE001 — proactive only; carry on
            pass

    restore_id = str((getattr(q.datasource, "options", None) or {}).get("restore_id") or "")
    key = (_snapshot_key(q, restore_id, merged_params, kept, spec, columns)
           if use_cache and restore_id else None)
    if key:
        hit = cache.get(key)
        if hit is not None:
            result = dict(hit, source="cache")
            if skipped:
                result["skipped_filters"] = skipped
            return result

    try:
        result = run(kept)
    except QueryError as exc:
        if not is_unknown_column_error(str(exc)):
            raise
        # Stale cached columns (query edited since last refresh) — refresh, drop
        # filters whose columns are gone, and retry once.
        refresh_dataset(dataset)
        columns = dataset.cached_columns or []
        kept, skipped = split(filters, columns)
        result = run(kept)
        key = None          # the column set changed under us: do not cache this one

    if key:
        cache.set(key, result, SNAPSHOT_TTL)
    result = dict(result, source="database")
    if skipped:
        result["skipped_filters"] = skipped
    return result


# ---------------------------------------------------------------- the SPA's requests
# Mirrors interactive-analytics-dse WidgetCard.jsx: which widgets the browser asks the
# database to aggregate, and with what body. warm_boards and perf_budget replay exactly
# these, so the cache they fill is the cache the browser reads.
SERVER_AGG_TYPES = frozenset({"bar", "line", "area", "pie"})
SERVER_AGGS = frozenset({"sum", "count", "avg", "min", "max"})


def widget_explore_bodies(widget) -> list[dict]:
    """The explore request bodies the dashboard UI sends for ``widget`` (with no
    dashboard filter selected)."""
    cfg = widget.config or {}
    if not widget.dataset_id or widget.widget_type == "studio":
        return []
    agg = str(cfg.get("rollup") or "sum").lower()
    if agg not in SERVER_AGGS or not (agg == "count" or cfg.get("value_field")):
        return []
    y = None if agg == "count" else cfg.get("value_field")
    filters = list(cfg.get("filters") or [])
    if widget.widget_type == "kpi":
        return [{"mode": "scalar", "y": y, "agg": agg, "filters": filters}]
    if (widget.widget_type in SERVER_AGG_TYPES and cfg.get("category_field")
            and not len(cfg.get("value_fields") or []) > 1):
        return [{"mode": "aggregate", "x": cfg["category_field"], "y": y, "agg": agg,
                 "group_by": cfg.get("group_field") or None,
                 "x_bucket": cfg.get("x_bucket") or None,
                 "filters": filters, "limit": 4000 if cfg.get("group_field") else 300}]
    return []


def board_requests(dashboard) -> list[tuple]:
    """(dataset, body) for every server-side call a load of ``dashboard`` makes."""
    out = []
    for widget in dashboard.widgets.select_related("dataset__query__datasource").all():
        for body in widget_explore_bodies(widget):
            out.append((widget.dataset, body))
    return out
