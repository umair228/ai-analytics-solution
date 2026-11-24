"""Server-side dataset exports: the file behind GET /api/datasets/<id>/export/
and the attachment of scheduled dataset reports.

The dataset's FULL query runs through the read-only executor (never the
cached ≤DSE_QUERY_MAX_ROWS preview), bounded by DIS_EXPORT_MAX_ROWS — or
DIS_EXPORT_PDF_MAX_ROWS for PDF. A larger result raises :class:`ExportTooLarge`
instead of being silently truncated. Auditing is the caller's job (the view and
the scheduler both write a fail-closed audit row before releasing the file).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from core.branding import footer, product_name
from core.report_render import (
    CONTENT_TYPES, FORMATS, ReportMeta, UnsupportedFormat, export_filename,
    format_stamp, format_timestamp, render,
)
from querybuilder.executor import (
    QueryError, execute_raw_sql_filtered, execute_spec, is_unknown_column_error,
)
from querybuilder.models import QueryDefinition

DATA_AS_OF_COLUMN = "data_as_of"


class ExportTooLarge(Exception):
    """The result exceeds the export ceiling for the requested format."""

    def __init__(self, fmt: str, limit: int):
        self.fmt, self.limit = fmt, limit
        if fmt == "pdf":
            msg = (f"This report has more than {limit:,} rows, the PDF limit. "
                   "Export it as XLSX or CSV, or narrow it with parameters or filters.")
        else:
            msg = (f"This report has more than {limit:,} rows, the export limit. "
                   "Narrow it with parameters or filters.")
        super().__init__(msg)


@dataclass
class DatasetExport:
    content: bytes
    content_type: str
    filename: str
    fmt: str
    row_count: int
    meta: ReportMeta
    params: dict = field(default_factory=dict)
    filters: list = field(default_factory=list)
    skipped_filters: list = field(default_factory=list)
    data_as_of: str | None = None       # the data_as_of column's value, if any

    def audit_detail(self, dataset) -> dict:
        return {
            "dataset": dataset.name,
            "format": self.fmt,
            "row_count": self.row_count,
            "params": self.params,
            "filters": self.filters,
            "skipped_filters": self.skipped_filters,
            "filename": self.filename,
            "bytes": len(self.content),
            "data_as_of": self.data_as_of,
            "last_refreshed_at": (dataset.last_refreshed_at.isoformat()
                                  if dataset.last_refreshed_at else None),
        }


def row_limit(fmt: str) -> int:
    return settings.DIS_EXPORT_PDF_MAX_ROWS if fmt == "pdf" else settings.DIS_EXPORT_MAX_ROWS


def user_label(user) -> str:
    """'Full Name (username)', or just the username."""
    if user is None:
        return ""
    full = (user.get_full_name() or "").strip()
    return f"{full} ({user.username})" if full else user.username


def _clean_filters(filters) -> list:
    """Only well-formed entries — a malformed filter must not 500 an export."""
    return [f for f in (filters or []) if isinstance(f, dict) and f.get("column")]


def run_full_query(dataset, *, params=None, filters=None, max_rows: int) -> dict:
    """The dataset's complete result (≤ ``max_rows`` + a ``truncated`` flag).

    ``params`` and ``filters`` mean exactly what they mean on the dataset's
    ``data`` endpoint: parameters merge over the query's declared defaults and
    the dataset's ``param_defaults``; filters become SQL predicates on output
    columns, a filter on a column the output does not have is skipped (and
    reported), and a stale cached column list heals itself with a refresh.

    Returns the executor payload plus ``params`` (effective), ``filters``
    (applied) and ``skipped_filters``.
    """
    from .services import refresh_dataset

    query = dataset.query
    merged = {**query.declared_param_defaults(),
              **(dataset.param_defaults or {}), **(params or {})}
    filters = _clean_filters(filters)

    if query.mode == QueryDefinition.Mode.RAW:
        def split(columns):
            kept = [f for f in filters if f["column"] in columns]
            skipped = sorted({str(f["column"]) for f in filters
                              if f["column"] not in columns})
            return kept, skipped

        def run(kept, columns):
            return execute_raw_sql_filtered(
                query.datasource, query.raw_sql, query.database or None,
                params=merged or None, filters=kept, columns=columns,
                max_rows=max_rows,
            )

        # Filters bind against the output-column allow-list (the cached column
        # names): populate it on first use.
        if filters and not dataset.cached_columns:
            refresh_dataset(dataset)
        kept, skipped = split(dataset.cached_columns or [])
        # A filter was dropped but the query changed since the last refresh:
        # the cached columns may just be stale — heal and re-split.
        if skipped and dataset.last_refreshed_at and query.updated_at \
                and query.updated_at > dataset.last_refreshed_at:
            try:
                refresh_dataset(dataset)
                kept, skipped = split(dataset.cached_columns or [])
            except Exception:  # noqa: BLE001 — proactive only
                pass
        try:
            result = run(kept, dataset.cached_columns or [])
        except QueryError as exc:
            if not (kept and is_unknown_column_error(str(exc))):
                raise
            refresh_dataset(dataset)  # stale columns: refresh, re-split, retry once
            kept, skipped = split(dataset.cached_columns or [])
            result = run(kept, dataset.cached_columns or [])
    else:
        # Visual-builder queries take no output filters; run the full spec
        # (its own LIMIT kept, the preview cap dropped).
        kept, skipped = [], sorted({str(f["column"]) for f in filters})
        result = execute_spec(query.datasource, query.spec, query.database or None,
                              max_rows=max_rows, full=True)
    result.update(params=merged, filters=kept, skipped_filters=skipped)
    return result


def data_as_of_value(columns, rows):
    """The latest value of a ``data_as_of`` output column (any case), if any."""
    idx = next((i for i, c in enumerate(columns)
                if str(c).lower() == DATA_AS_OF_COLUMN), None)
    if idx is None:
        return None
    values = [r[idx] for r in rows if idx < len(r) and r[idx] not in (None, "")]
    return max(values, key=str) if values else None


def source_freshness(dataset):
    """When the source data was taken, from the data source's own freshness query.

    A data source may declare ``options["freshness_sql"]``: a read-only query returning one
    value, e.g. the backup time of a nightly restored reporting copy. Exports then state
    *that* as "Data as of" instead of the time DIS last refreshed its cache. Optional and
    fail-soft: no option, a failing query or an empty result returns None, and the export
    falls back to the refresh time exactly as before.
    """
    query = getattr(dataset, "query", None)
    datasource = getattr(query, "datasource", None)
    sql = str(((getattr(datasource, "options", None) or {}).get("freshness_sql") or "")).strip()
    if not sql:
        return None
    try:
        from querybuilder.executor import assert_read_only, execute_raw_sql

        assert_read_only(sql)
        result = execute_raw_sql(datasource, sql, database=getattr(query, "database", None) or None,
                                 max_rows=1)
        rows = result.get("rows") or []
        return rows[0][0] if rows and rows[0] and rows[0][0] not in (None, "") else None
    except Exception:  # noqa: BLE001 - a freshness stamp must never break an export
        return None


def _describe_filter(f: dict) -> str:
    col, ftype = f.get("column"), f.get("type")
    if ftype == "compare":
        return f"{col} {f.get('op')} {f.get('value')}"
    if ftype in ("date-range", "number-range"):
        lo = f.get("from", f.get("min"))
        hi = f.get("to", f.get("max"))
        return f"{col} {'' if lo in (None, '') else lo}..{'' if hi in (None, '') else hi}"
    if ftype == "text-search":
        return f"{col} contains {f.get('value')}"
    values = f.get("values")
    if values is None:
        values = [f.get("value")]
    return f"{col} {'not in' if f.get('not') else 'in'} {', '.join(map(str, values))}"


def build_meta(dataset, *, row_count: int, generated_by: str, generated_at,
               data_as_of=None, params=None, filters=None, skipped=None,
               note: str = "") -> ReportMeta:
    refreshed = format_timestamp(dataset.last_refreshed_at) or "not yet refreshed"
    source_stamp = format_stamp(data_as_of) if data_as_of is not None else ""
    if source_stamp:
        # The source's own time (e.g. the nightly backup) is what the numbers describe.
        lines = [("Data as of", source_stamp), ("Refreshed", refreshed)]
    else:
        lines = [("Data as of", refreshed)]
    if dataset.description:
        lines.append(("Description", dataset.description))
    lines.append(("Generated by", generated_by or "-"))
    lines.append(("Generated at", format_timestamp(generated_at)))
    lines.append(("Rows", f"{row_count:,}"))
    shown = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    if shown:
        lines.append(("Parameters", ", ".join(f"{k}={v}" for k, v in shown.items())))
    if filters:
        lines.append(("Filters", "; ".join(_describe_filter(f) for f in filters)))
    if skipped:
        lines.append(("Filters not applied", ", ".join(skipped)))
    compact = source_stamp or refreshed
    return ReportMeta(
        title=dataset.name,
        product_name=product_name(),
        footer=footer(),
        lines=lines,
        data_as_of=compact,
        generated_by=generated_by,
        generated_at=format_timestamp(generated_at),
        note=note,
    )


def export_dataset(dataset, fmt: str, *, generated_by: str, params=None,
                   filters=None, now=None) -> DatasetExport:
    """Run the dataset's full query and render it as ``fmt`` (csv|xlsx|pdf)."""
    fmt = (fmt or "").lower()
    if fmt not in FORMATS:
        raise UnsupportedFormat(
            f"Unsupported export format {fmt!r}. Use one of: {', '.join(FORMATS)}.")
    limit = row_limit(fmt)
    try:
        result = run_full_query(dataset, params=params, filters=filters, max_rows=limit)
    except QueryError:
        raise
    except Exception as exc:  # e.g. a misconfigured data source: a query problem
        raise QueryError(str(exc)) from exc
    if result.get("truncated"):
        raise ExportTooLarge(fmt, limit)

    now = now or timezone.now()
    columns, rows = result["columns"], result["rows"]
    as_of = data_as_of_value(columns, rows)
    if as_of is None:
        as_of = source_freshness(dataset)
    meta = build_meta(
        dataset, row_count=len(rows), generated_by=generated_by, generated_at=now,
        data_as_of=as_of, params=result["params"], filters=result["filters"],
        skipped=result["skipped_filters"],
    )
    content = render(fmt, columns, rows, meta)
    return DatasetExport(
        content=content,
        content_type=CONTENT_TYPES[fmt],
        filename=export_filename(dataset.name, fmt, now, fallback=f"dataset-{dataset.pk}"),
        fmt=fmt,
        row_count=len(rows),
        meta=meta,
        params=_json_safe(result["params"]),
        filters=_json_safe(result["filters"]),
        skipped_filters=result["skipped_filters"],
        data_as_of=None if as_of is None else str(as_of),
    )


def _json_safe(value):
    """Round-trip through JSON so the audit detail always serialises."""
    return json.loads(json.dumps(value, default=str))
