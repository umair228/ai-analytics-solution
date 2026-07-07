from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from analytics.engine import (
    aggregate as run_aggregate,
    build_dataframe,
    column_statistics,
    compare as run_compare,
    correlation_matrix,
    df_to_rows,
    rolling_window,
)
from analytics.predict import auto_summary, detect_anomalies, forecast as run_forecast
from analytics.profiling import profile_dataset
from analytics.semantic import SemanticError, answer_question, suggest_questions
from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove, IsOwnerOrSharedReadOnly

from django.utils import timezone

from .models import AlertEvent, Dataset, DatasetAlert, DatasetReport
from .serializers import AlertEventSerializer, DatasetAlertSerializer, DatasetReportSerializer, DatasetSerializer
from .services import refresh_dataset


def _error(exc):
    return Response({"error": True, "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _infer_output_filter_type(name: str, sample_values: list) -> str:
    """Pick a filter control for a dataset output column from its name + a
    sample of values (cached rows carry no SQL type, so we sniff the data)."""
    from connections.introspection import (
        FILTER_CATEGORY, FILTER_DATE, FILTER_NUMBER, FILTER_TEXT,
        classify_filter_type,
    )
    import datetime as _dt

    # Name-based hint first (catches date columns even when values are strings).
    by_name = classify_filter_type("", name)
    if by_name == FILTER_DATE:
        return FILTER_DATE

    non_null = [v for v in sample_values if v not in (None, "")]
    if not non_null:
        return by_name
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return FILTER_NUMBER
    if all(isinstance(v, (_dt.date, _dt.datetime)) for v in non_null):
        return FILTER_DATE
    # Looks like an ISO date string?
    if all(isinstance(v, str) and len(v) >= 8 and v[:4].isdigit() and v[4] in "-/"
           for v in non_null):
        return FILTER_DATE
    # Low cardinality → dropdown, else free-text search.
    if len({str(v) for v in non_null}) <= 25:
        return FILTER_CATEGORY
    return FILTER_TEXT


class DatasetViewSet(viewsets.ModelViewSet):
    """Reusable, cached result sets backed by saved queries, with a
    pandas-powered statistics / aggregation / comparison / prediction engine."""

    serializer_class = DatasetSerializer
    permission_classes = [IsAnalystOrAbove, IsOwnerOrSharedReadOnly]

    def get_queryset(self):
        user = self.request.user
        qs = Dataset.objects.select_related(
            "query", "query__datasource", "owner"
        ).prefetch_related("shared_with")
        if user.is_admin:
            return qs
        return qs.filter(Q(owner=user) | Q(shared_with=user)).distinct()

    def get_permissions(self):
        if self.action in (
            "list", "retrieve", "data", "refresh",
            "statistics", "aggregate", "compare",
            "forecast", "anomalies", "summary", "ask", "profile",
            "correlation", "rolling", "filter_options", "distinct_values",
            "explore", "suggest_filters",
        ):
            return [IsAuthenticated()]
        return [perm() for perm in self.permission_classes]

    def perform_create(self, serializer):
        dataset = serializer.save()
        record_audit(self.request, AuditLog.Action.CREATE, target_type="Dataset",
                     target_id=dataset.id, summary=f"Created dataset '{dataset.name}'")

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        dataset = self.get_object()
        try:
            refresh_dataset(dataset)
        except Exception as exc:  # noqa: BLE001
            dataset.last_error = str(exc)
            dataset.save(update_fields=["last_error"])
            return _error(exc)
        record_audit(request, AuditLog.Action.QUERY, target_type="Dataset",
                     target_id=dataset.id, summary=f"Refreshed dataset '{dataset.name}'")
        return Response(DatasetSerializer(dataset).data)

    @action(detail=True, methods=["get"])
    def data(self, request, pk=None):
        """Dataset rows, including any calculated fields.

        Pass ?params={"key":"value",...} (URL-encoded JSON) to inject query
        parameters into a parameterised raw-SQL query on the fly without
        updating the cached result.
        """
        dataset = self.get_object()
        import json
        raw_params = request.query_params.get("params")
        live_params = None
        if raw_params:
            try:
                live_params = json.loads(raw_params)
            except (ValueError, TypeError):
                live_params = None

        raw_filters = request.query_params.get("filters")
        live_filters = None
        if raw_filters:
            try:
                parsed = json.loads(raw_filters)
                live_filters = parsed if isinstance(parsed, list) else None
            except (ValueError, TypeError):
                live_filters = None

        # ?limit= pulls more rows than the cached preview (bounded server-side).
        limit = None
        if request.query_params.get("limit"):
            from querybuilder.executor import EXPLORE_MAX_PAGE
            try:
                limit = max(1, min(int(request.query_params["limit"]),
                                   EXPLORE_MAX_PAGE))
            except (TypeError, ValueError):
                limit = None

        if limit and not (live_params or live_filters):
            # Fresh pull at the requested size, bypassing the capped cache.
            try:
                from querybuilder.executor import execute_raw_sql, execute_spec
                from querybuilder.models import QueryDefinition
                q = dataset.query
                merged = dataset.param_defaults or {}
                if q.mode == QueryDefinition.Mode.RAW:
                    result = execute_raw_sql(q.datasource, q.raw_sql,
                                             q.database or None,
                                             params=merged or None,
                                             max_rows=limit)
                else:
                    result = execute_spec(q.datasource, q.spec,
                                          q.database or None, max_rows=limit)
                return Response({**result,
                                 "last_refreshed_at": dataset.last_refreshed_at})
            except Exception as exc:  # noqa: BLE001
                return _error(exc)

        if live_params or live_filters:
            # Run fresh with the supplied params / filters; bypass the cache.
            try:
                from querybuilder.executor import (
                    execute_raw_sql_filtered, execute_spec,
                )
                from querybuilder.models import QueryDefinition
                q = dataset.query
                merged = {**(dataset.param_defaults or {}), **(live_params or {})}
                if q.mode == QueryDefinition.Mode.RAW:
                    result = execute_raw_sql_filtered(
                        q.datasource, q.raw_sql, q.database or None,
                        params=merged, filters=live_filters,
                        columns=dataset.cached_columns or [],
                        max_rows=limit,
                    )
                else:
                    result = execute_spec(q.datasource, q.spec, q.database or None)
                return Response({**result, "last_refreshed_at": dataset.last_refreshed_at})
            except Exception as exc:  # noqa: BLE001
                return _error(exc)

        if request.query_params.get("refresh") == "1":
            try:
                refresh_dataset(dataset)
            except Exception as exc:  # noqa: BLE001
                return _error(exc)
        try:
            df = build_dataframe(dataset)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response({
            "columns": [str(c) for c in df.columns],
            "rows": df_to_rows(df),
            "row_count": int(len(df)),
            "last_refreshed_at": dataset.last_refreshed_at,
        })

    @action(detail=True, methods=["post"])
    def explore(self, request, pk=None):
        """Server-side exploration over the FULL dataset result — never limited
        by the cached-preview row cap. Filters become SQL predicates and the
        database computes counts / extents / aggregations / row pages.

        Body: {"mode": "count"|"extent"|"aggregate"|"rows",
               "filters": [...], "params": {...},
               "x", "y", "agg", "group_by", "column", "limit", "offset"}
        """
        from querybuilder.executor import QueryError, execute_dataset_query
        from querybuilder.models import QueryDefinition

        dataset = self.get_object()
        q = dataset.query
        if q is None or q.mode != QueryDefinition.Mode.RAW:
            return Response(
                {"error": True, "unsupported": True,
                 "detail": "Full-data explore requires a raw-SQL dataset; "
                           "the client falls back to the cached preview."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The output-column allow-list comes from the cached result; populate
        # it on first use so a never-refreshed dataset still works.
        if not dataset.cached_columns:
            try:
                refresh_dataset(dataset)
            except Exception as exc:  # noqa: BLE001
                return _error(exc)

        body = request.data or {}
        spec = {k: body.get(k)
                for k in ("mode", "x", "y", "agg", "group_by", "x_bucket",
                          "column", "limit", "offset")}
        merged_params = {**(dataset.param_defaults or {}),
                         **(body.get("params") or {})}
        try:
            result = execute_dataset_query(
                q.datasource, q.raw_sql, q.database or None,
                params=merged_params or None,
                filters=body.get("filters") or [],
                columns=dataset.cached_columns or [],
                spec=spec,
            )
        except QueryError as exc:
            return _error(exc)
        result["source"] = "database"
        return Response(result)

    @action(detail=True, methods=["get"], url_path="suggest-filters")
    def suggest_filters(self, request, pk=None):
        """AI-recommended filters for this dataset.

        Heuristics over the cached sample pick sensible candidates (a date
        window, low-cardinality dimensions, key measures); when the LLM is
        configured it selects/re-ranks them and explains why each is useful.
        """
        from connections.introspection import (
            FILTER_CATEGORY, FILTER_DATE, FILTER_NUMBER,
        )

        dataset = self.get_object()
        cols = dataset.cached_columns or []
        sample = (dataset.cached_rows or [])[:500]

        candidates = []
        seen_date = False
        for idx, name in enumerate(cols):
            values = [r[idx] for r in sample
                      if idx < len(r) and r[idx] not in (None, "")]
            ftype = _infer_output_filter_type(name, values)
            label = name.lower().replace("_", " ")
            if ftype == FILTER_DATE and not seen_date:
                seen_date = True
                candidates.append({
                    "column": name, "type": FILTER_DATE,
                    "reason": "Narrow the analysis to a specific time window.",
                })
            elif ftype == FILTER_CATEGORY:
                distinct = sorted({str(v) for v in values})
                candidates.append({
                    "column": name, "type": FILTER_CATEGORY,
                    "options": distinct[:25],
                    "reason": f"Slice by {label} ({len(distinct)} values).",
                })
            elif ftype == FILTER_NUMBER:
                nums = [v for v in values
                        if isinstance(v, (int, float)) and not isinstance(v, bool)]
                if nums:
                    candidates.append({
                        "column": name, "type": FILTER_NUMBER,
                        "min": min(nums), "max": max(nums),
                        "reason": f"Restrict {label} to a value range.",
                    })

        suggestions, source = candidates, "heuristic"
        try:
            from ai.client import is_configured, suggest_dataset_filters
            if candidates and is_configured():
                from ai.context import build_dataset_context
                picked = suggest_dataset_filters(
                    build_dataset_context(dataset), candidates)
                by_col = {c["column"]: c for c in candidates}
                validated = []
                for p in picked:
                    base = by_col.get((p or {}).get("column"))
                    if not base:
                        continue
                    merged = {**base}
                    reason = str(p.get("reason") or "").strip()
                    if reason:
                        merged["reason"] = reason
                    validated.append(merged)
                if validated:
                    suggestions, source = validated, "ai"
        except Exception:  # noqa: BLE001 — AI is best-effort; heuristics stand
            pass

        return Response({"suggestions": suggestions[:6], "source": source})

    @action(detail=True, methods=["get"], url_path="filter-options")
    def filter_options(self, request, pk=None):
        """Discover filterable columns for this dataset.

        Walks the dataset's result columns *and* introspects the underlying
        source tables in the live database, classifying each column into a
        filter control (date-range / dropdown / number-range / text-search).
        This is what lets the dashboard "go through the DB and set up filters".
        """
        from connections import introspection as I
        from querybuilder.models import QueryDefinition

        dataset = self.get_object()
        q = dataset.query
        ds = q.datasource
        database = q.database or None

        # 1) Columns the dataset already outputs — always safe to filter on
        #    (applied server-side via a subquery wrapper). Type inferred from
        #    the cached sample rows.
        output_cols = []
        cols = dataset.cached_columns or []
        sample = (dataset.cached_rows or [])[:200]
        for idx, name in enumerate(cols):
            values = [r[idx] for r in sample if idx < len(r) and r[idx] is not None]
            ftype = _infer_output_filter_type(name, values)
            output_cols.append({
                "column": name, "filter_type": ftype, "source": "output",
            })

        # 2) Columns discovered by introspecting the source tables (so users
        #    can filter on dimensions like SAMPLE.LOGIN_DATE that aren't in the
        #    aggregated output — wired through query parameters).
        source_cols = []
        tables = []
        if q.mode == QueryDefinition.Mode.RAW and ds.source_type in I.SourceType.DATABASE_TYPES:
            try:
                for t in I.source_tables_from_sql(q.raw_sql):
                    classified = I.classify_columns(
                        ds, t["table"], schema=t["schema"], database=database)
                    tables.append({"table": t["table"], "schema": t["schema"],
                                   "column_count": len(classified)})
                    for c in classified:
                        c["schema"] = t["schema"]
                        source_cols.append(c)
            except Exception as exc:  # noqa: BLE001 — discovery is best-effort
                return Response({
                    "output_columns": output_cols, "source_columns": [],
                    "tables": [], "params": q.parameters or [],
                    "discovery_error": str(exc),
                })

        return Response({
            "output_columns": output_cols,
            "source_columns": source_cols,
            "tables": tables,
            "params": q.parameters or [],
        })

    @action(detail=True, methods=["get"], url_path="distinct-values")
    def distinct_values(self, request, pk=None):
        """Live distinct values / min-max for one column — populates a filter
        control. ?column=NAME [&table=SAMPLE&schema=dbo] (table → source
        introspection; omitted → derive from the dataset's cached rows)."""
        from connections import introspection as I

        dataset = self.get_object()
        column = request.query_params.get("column")
        if not column:
            return _error("Query parameter 'column' is required.")
        table = request.query_params.get("table")
        q = dataset.query

        if table:
            try:
                return Response(I.column_profile(
                    q.datasource, table, column,
                    schema=request.query_params.get("schema"),
                    database=q.database or None,
                ))
            except Exception as exc:  # noqa: BLE001
                return _error(exc)

        # Fall back to distinct values from the cached output rows.
        cols = dataset.cached_columns or []
        if column not in cols:
            return _error(f"Column '{column}' is not in this dataset.")
        idx = cols.index(column)
        seen = []
        seen_set = set()
        for r in dataset.cached_rows or []:
            if idx < len(r) and r[idx] is not None:
                key = str(r[idx])
                if key not in seen_set:
                    seen_set.add(key)
                    seen.append(r[idx])
        return Response({
            "column": column, "filter_type": "dropdown",
            "distinct_count": len(seen),
            "values": sorted(seen, key=lambda v: str(v))[:200],
        })

    @action(detail=True, methods=["get"])
    def statistics(self, request, pk=None):
        """Descriptive statistics for every column of the dataset."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(column_statistics(df))

    @action(detail=True, methods=["post"])
    def aggregate(self, request, pk=None):
        """Group the dataset by a column and aggregate a measure."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
            result = run_aggregate(
                df,
                request.data.get("group_by"),
                request.data.get("measure"),
                request.data.get("aggregation", "count"),
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(result)

    @action(detail=True, methods=["post"])
    def compare(self, request, pk=None):
        """Comparative analysis of a measure across a dimension."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
            result = run_compare(
                df,
                request.data.get("dimension"),
                request.data.get("measure"),
                request.data.get("aggregation", "count"),
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(result)

    @action(detail=True, methods=["post"])
    def forecast(self, request, pk=None):
        """Forecast a numeric column with a trend summary."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
            periods = int(request.data.get("periods", 6) or 6)
            result = run_forecast(
                df, request.data.get("value_column"), max(1, min(periods, 60))
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(result)

    @action(detail=True, methods=["post"])
    def anomalies(self, request, pk=None):
        """Detect outliers in a numeric column."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
            result = detect_anomalies(
                df,
                request.data.get("value_column"),
                request.data.get("method", "zscore"),
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(result)

    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        """Auto-generated per-column insights for the dataset."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(auto_summary(df))

    @action(detail=True, methods=["get", "post"])
    def ask(self, request, pk=None):
        """Natural-language Q&A over the dataset (deterministic semantic engine).

        GET  -> suggested example questions for this dataset.
        POST -> answer a question ({"question": "..."}).
        """
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

        if request.method == "GET":
            return Response({"suggestions": suggest_questions(df, dataset.name)})

        question = (request.data.get("question") or "").strip()
        try:
            result = answer_question(df, question, dataset.name)
        except SemanticError as exc:
            return Response({
                "understood": False,
                "answer": str(exc),
                "question": question,
                "suggestions": suggest_questions(df, dataset.name),
            })
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        record_audit(request, AuditLog.Action.QUERY, target_type="Dataset",
                     target_id=dataset.id, summary=f"Asked: {question[:60]}")
        return Response(result)

    @action(detail=True, methods=["get"])
    def profile(self, request, pk=None):
        """Full data-quality profile: per-column distributions and quality flags."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(profile_dataset(df))

    @action(detail=True, methods=["get"])
    def correlation(self, request, pk=None):
        """Pairwise Pearson/Spearman correlation matrix for numeric columns."""
        dataset = self.get_object()
        method = request.query_params.get("method", "pearson")
        if method not in ("pearson", "spearman", "kendall"):
            method = "pearson"
        try:
            df = build_dataframe(dataset)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(correlation_matrix(df, method=method))

    @action(detail=True, methods=["post"])
    def rolling(self, request, pk=None):
        """Rolling window analytics — moving average/sum/min/max and pct change."""
        dataset = self.get_object()
        try:
            df = build_dataframe(dataset)
            result = rolling_window(
                df,
                value_column=request.data.get("value_column"),
                index_column=request.data.get("index_column"),
                window=int(request.data.get("window", 7) or 7),
                func=request.data.get("function", "mean"),
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(result)


class AlertViewSet(viewsets.ModelViewSet):
    """CRUD for dataset threshold alerts."""

    serializer_class = DatasetAlertSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = DatasetAlert.objects.select_related("dataset", "owner").prefetch_related("events")
        if user.is_admin:
            return qs
        return qs.filter(
            Q(owner=user) | Q(dataset__owner=user) | Q(dataset__shared_with=user)
        ).distinct()

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        alert = self.get_object()
        alert.is_active = not alert.is_active
        alert.save(update_fields=["is_active", "updated_at"])
        return Response(DatasetAlertSerializer(alert, context={"request": request}).data)


class AlertEventViewSet(viewsets.ReadOnlyModelViewSet):
    """List and acknowledge alert events."""

    serializer_class = AlertEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = AlertEvent.objects.select_related("alert", "alert__dataset", "acknowledged_by")
        if user.is_admin:
            return qs
        return qs.filter(
            Q(alert__owner=user) | Q(alert__dataset__owner=user) |
            Q(alert__dataset__shared_with=user)
        ).distinct()

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        event = self.get_object()
        if not event.acknowledged:
            event.acknowledged = True
            event.acknowledged_at = timezone.now()
            event.acknowledged_by = request.user
            event.save(update_fields=["acknowledged", "acknowledged_at", "acknowledged_by"])
        return Response(AlertEventSerializer(event, context={"request": request}).data)

    @action(detail=False, methods=["post"])
    def acknowledge_all(self, request):
        now = timezone.now()
        updated = self.get_queryset().filter(acknowledged=False).update(
            acknowledged=True, acknowledged_at=now, acknowledged_by=request.user,
        )
        return Response({"acknowledged": updated})


class DatasetReportViewSet(viewsets.ModelViewSet):
    """CRUD for scheduled email reports (dataset CSV / analysis run / chart),
    plus a one-off ``email-now`` action for ad-hoc chart sharing."""
    serializer_class = DatasetReportSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = DatasetReport.objects.select_related("dataset", "analysis_run")
        if self.request.user.is_admin:
            return qs
        return qs.filter(
            Q(owner=self.request.user) | Q(dataset__owner=self.request.user)
        ).distinct()

    @action(detail=False, methods=["post"], url_path="email-now")
    def email_now(self, request):
        """Send a one-off email now (no schedule): a chart image (PNG/JPEG data
        URL) and/or an exported analysis run / chart-data file."""
        import base64
        import binascii

        from django.core.mail import EmailMessage

        d = request.data
        recipients = [e.strip() for e in (d.get("recipient_emails") or "").split(",") if e.strip()]
        if not recipients:
            return Response({"error": True, "detail": "At least one recipient email is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        subject = (d.get("subject") or "DSE chart").strip()
        safe = "".join(c if c.isalnum() else "_" for c in subject)[:60] or "dse"
        msg = EmailMessage(subject=f"[DSE] {subject}",
                           body=(d.get("note") or f"{subject} (shared from DSE)."),
                           to=recipients)

        attached = False
        image_data_url = d.get("image_data_url") or ""
        if "," in image_data_url:
            header, b64 = image_data_url.split(",", 1)
            ext = "jpeg" if ("jpeg" in header or "jpg" in header) else "png"
            try:
                msg.attach(f"{safe}.{ext}", base64.b64decode(b64), f"image/{ext}")
                attached = True
            except (binascii.Error, ValueError):
                pass

        fmt = (d.get("format") or "").lower()
        if fmt:
            from ai.exporters import ExportUnavailable, export
            envelope = None
            if d.get("analysis_run"):
                from analytics.models import AnalysisRun
                from analytics.reporting import run_to_envelope
                run = AnalysisRun.objects.filter(pk=d["analysis_run"]).first()
                if run and run.accessible_by(request.user):
                    envelope = run_to_envelope(run)
            elif d.get("chart_config"):
                from analytics.reporting import chart_to_envelope
                envelope = chart_to_envelope(d["chart_config"])
            if envelope is not None:
                try:
                    data, ctype, _ = export(envelope, fmt)
                    msg.attach(f"{safe}.{fmt}", data, ctype)
                    attached = True
                except ExportUnavailable:
                    pass

        try:
            msg.send()
        except Exception as exc:  # noqa: BLE001
            return Response({"error": True, "detail": f"Email failed: {exc}"},
                            status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, AuditLog.Action.QUERY, target_type="Email",
                     summary=f"Emailed '{subject}' to {len(recipients)} recipient(s)")
        return Response({"ok": True, "recipients": len(recipients), "attached": attached})
