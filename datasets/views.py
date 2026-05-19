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

from .models import AlertEvent, Dataset, DatasetAlert
from .serializers import AlertEventSerializer, DatasetAlertSerializer, DatasetSerializer
from .services import refresh_dataset


def _error(exc):
    return Response({"error": True, "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


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

        if live_params:
            # Run fresh with the supplied params; bypass the cache.
            try:
                from querybuilder.executor import execute_raw_sql, execute_spec
                from querybuilder.models import QueryDefinition
                q = dataset.query
                merged = {**(dataset.param_defaults or {}), **live_params}
                if q.mode == QueryDefinition.Mode.RAW:
                    result = execute_raw_sql(q.datasource, q.raw_sql,
                                            q.database or None, params=merged)
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
