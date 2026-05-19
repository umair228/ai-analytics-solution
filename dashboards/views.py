from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove, IsOwnerOrSharedReadOnly

from .models import Dashboard, Widget
from .serializers import DashboardSerializer, WidgetSerializer


class DashboardViewSet(viewsets.ModelViewSet):
    """Interactive dashboards with their widgets."""

    serializer_class = DashboardSerializer
    permission_classes = [IsAnalystOrAbove, IsOwnerOrSharedReadOnly]

    def get_queryset(self):
        user = self.request.user
        qs = Dashboard.objects.select_related("owner").prefetch_related(
            "widgets", "widgets__dataset", "shared_with"
        )
        if user.is_admin:
            return qs
        return qs.filter(Q(owner=user) | Q(shared_with=user)).distinct()

    def get_permissions(self):
        if self.action in ("list", "retrieve", "layout"):
            return [IsAuthenticated()]
        return [perm() for perm in self.permission_classes]

    def perform_create(self, serializer):
        dashboard = serializer.save()
        record_audit(self.request, AuditLog.Action.CREATE, target_type="Dashboard",
                     target_id=dashboard.id, summary=f"Created dashboard '{dashboard.name}'")

    @action(detail=True, methods=["post"])
    def layout(self, request, pk=None):
        """Bulk-save widget positions after a drag/resize."""
        dashboard = self.get_object()
        for item in request.data.get("widgets", []):
            Widget.objects.filter(id=item.get("id"), dashboard=dashboard).update(
                x=item.get("x", 0), y=item.get("y", 0),
                w=item.get("w", 4), h=item.get("h", 6),
            )
        fresh = (
            Dashboard.objects.prefetch_related("widgets", "widgets__dataset")
            .get(pk=dashboard.pk)
        )
        return Response(
            DashboardSerializer(fresh, context={"request": request}).data
        )


class WidgetViewSet(viewsets.ModelViewSet):
    """CRUD for dashboard widgets."""

    serializer_class = WidgetSerializer
    permission_classes = [IsAnalystOrAbove]

    def get_queryset(self):
        user = self.request.user
        qs = Widget.objects.select_related("dashboard", "dataset")
        if user.is_admin:
            return qs
        return qs.filter(
            Q(dashboard__owner=user) | Q(dashboard__shared_with=user)
        ).distinct()

    def _ensure_access(self, dashboard):
        user = self.request.user
        if not (
            user.is_admin
            or dashboard.owner_id == user.id
            or dashboard.shared_with.filter(pk=user.pk).exists()
        ):
            raise PermissionDenied("You do not have access to this dashboard.")

    def perform_create(self, serializer):
        self._ensure_access(serializer.validated_data["dashboard"])
        serializer.save()

    def perform_update(self, serializer):
        self._ensure_access(serializer.instance.dashboard)
        serializer.save()
