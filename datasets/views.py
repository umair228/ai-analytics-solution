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
)
from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove, IsOwnerOrSharedReadOnly

from .models import Dataset
from .serializers import DatasetSerializer
from .services import refresh_dataset


def _error(exc):
    return Response({"error": True, "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class DatasetViewSet(viewsets.ModelViewSet):
    """Reusable, cached result sets backed by saved queries, with a
    pandas-powered statistics / aggregation / comparison engine."""

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
        """Dataset rows, including any calculated fields."""
        dataset = self.get_object()
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
