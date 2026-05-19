from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import AuditLog
from .permissions import IsAdmin


class AuditLogSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = [
            "id", "user", "username", "action", "target_type",
            "target_id", "summary", "detail", "ip_address", "created_at",
        ]


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Paginated, filterable audit trail — admins see all, others see own."""

    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = AuditLog.objects.select_related("user")
        if not user.is_admin:
            qs = qs.filter(user=user)
        action = self.request.query_params.get("action")
        target_type = self.request.query_params.get("target_type")
        search = self.request.query_params.get("search")
        if action:
            qs = qs.filter(action=action)
        if target_type:
            qs = qs.filter(target_type=target_type)
        if search:
            qs = qs.filter(summary__icontains=search)
        return qs


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe."""
    return Response(
        {
            "status": "ok",
            "service": "dse-api",
            "name": "Interactive Reporting & AI Analytics Solution",
            "time": timezone.now().isoformat(),
        }
    )
