from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove, IsOwnerOrSharedReadOnly

from .models import Dataset
from .serializers import DatasetSerializer
from .services import refresh_dataset


class DatasetViewSet(viewsets.ModelViewSet):
    """Reusable, cached result sets backed by saved queries."""

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
        if self.action in ("list", "retrieve", "data", "refresh"):
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
            return Response({"error": True, "detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, AuditLog.Action.QUERY, target_type="Dataset",
                     target_id=dataset.id, summary=f"Refreshed dataset '{dataset.name}'")
        return Response(DatasetSerializer(dataset).data)

    @action(detail=True, methods=["get"])
    def data(self, request, pk=None):
        dataset = self.get_object()
        force = request.query_params.get("refresh") == "1"
        if force or not dataset.last_refreshed_at:
            try:
                refresh_dataset(dataset)
            except Exception as exc:  # noqa: BLE001
                return Response({"error": True, "detail": str(exc)},
                                status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "columns": dataset.cached_columns,
            "rows": dataset.cached_rows,
            "row_count": dataset.row_count,
            "last_refreshed_at": dataset.last_refreshed_at,
        })
