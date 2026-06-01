import secrets

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import generics, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.audit import record_audit
from core.models import AuditLog
from core.permissions import IsAnalystOrAbove, IsOwnerOrSharedReadOnly

from .models import Dashboard, DashboardShareToken, Widget
from .serializers import DashboardSerializer, WidgetSerializer


class DashboardViewSet(viewsets.ModelViewSet):
    """Interactive dashboards with their widgets."""

    serializer_class = DashboardSerializer
    permission_classes = [IsAnalystOrAbove, IsOwnerOrSharedReadOnly]

    def get_queryset(self):
        user = self.request.user
        qs = Dashboard.objects.select_related("owner").prefetch_related(
            "widgets", "widgets__dataset",
            "widgets__dataset__query__datasource", "shared_with"
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

    @action(detail=True, methods=["post"], url_path="clone", url_name="clone")
    def clone(self, request, pk=None):
        """Duplicate a dashboard with all its widgets."""
        original = self.get_object()
        with transaction.atomic():
            cloned = Dashboard.objects.create(
                name=f"Copy of {original.name}",
                description=original.description,
                owner=request.user,
                visibility=Dashboard.Visibility.PRIVATE,
                filters=original.filters,
            )
            for widget in original.widgets.all():
                Widget.objects.create(
                    dashboard=cloned,
                    title=widget.title,
                    widget_type=widget.widget_type,
                    dataset=widget.dataset,
                    config=widget.config,
                    x=widget.x, y=widget.y,
                    w=widget.w, h=widget.h,
                    order=widget.order,
                )
        return Response(
            DashboardSerializer(cloned, context={"request": request}).data,
            status=201,
        )

    @action(detail=True, methods=["post", "get"], url_path="share-token", url_name="share-token")
    def share_token(self, request, pk=None):
        """GET returns existing token (or null). POST creates one if absent."""
        dashboard = self.get_object()
        if request.method == "POST":
            token_obj, _ = DashboardShareToken.objects.get_or_create(
                dashboard=dashboard,
                defaults={"token": secrets.token_urlsafe(32), "created_by": request.user},
            )
            return Response({"token": token_obj.token})
        try:
            token_obj = DashboardShareToken.objects.get(dashboard=dashboard)
            return Response({"token": token_obj.token})
        except DashboardShareToken.DoesNotExist:
            return Response({"token": None})


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


class PublicDashboardView(generics.RetrieveAPIView):
    """Read a dashboard by share token — no authentication required."""
    serializer_class = DashboardSerializer
    permission_classes = [AllowAny]
    authentication_classes = []

    def get_object(self):
        token_str = self.kwargs["token"]
        try:
            tok = DashboardShareToken.objects.select_related("dashboard").get(token=token_str)
        except DashboardShareToken.DoesNotExist:
            raise NotFound("Invalid or revoked share link.")
        if tok.expires_at and tok.expires_at < timezone.now():
            raise PermissionDenied("This share link has expired.")
        return tok.dashboard
