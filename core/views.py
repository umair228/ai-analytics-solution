from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import api_view, authentication_classes, permission_classes
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
@authentication_classes([])
@permission_classes([AllowAny])
def branding_view(request):
    """Deployment branding for the SPA shell (public: the login page needs it).

    {product_name, short_name, tagline, footer, profile} — profile is "full"
    or "reporting" (a reporting-only deployment). No authentication is
    attempted, so a stale token in the browser can't turn this into a 401."""
    from .branding import branding
    return Response(branding())


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


# Files the BROWSER builds (board PDF, chart image, explorer / query CSV or Excel). The SPA
# calls this before it builds one and builds it only on 201, so these exports are audited like
# the server-rendered ones (R1: 100% of exports audited; BRD 5.7 "log exports").
CLIENT_EXPORT_FORMATS = ("csv", "xlsx", "pdf", "png", "jpeg")
CLIENT_EXPORT_TARGETS = ("Dashboard", "Dataset", "Query", "Page")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def client_export_audit(request):
    """Commit an EXPORT audit row for a file the browser is about to create.

    Body: {format, filename, target_type, target_id, page, rows}. 201 {audit_id} means the
    row is committed; 503 means it could not be written and the browser must not create the
    file (a reporting deployment refuses; the full platform may proceed best-effort)."""
    from .audit import EXPORT, AuditWriteError, record_audit_strict

    body = request.data if isinstance(request.data, dict) else {}
    fmt = str(body.get("format") or "").strip().lower()
    if fmt not in CLIENT_EXPORT_FORMATS:
        return Response({"detail": f"format: must be one of {', '.join(CLIENT_EXPORT_FORMATS)}."},
                        status=400)
    target_type = str(body.get("target_type") or "Page")
    if target_type not in CLIENT_EXPORT_TARGETS:
        return Response({"detail": f"target_type: must be one of "
                                   f"{', '.join(CLIENT_EXPORT_TARGETS)}."}, status=400)
    filename = str(body.get("filename") or "").strip()[:200]
    target_id = str(body.get("target_id") or "").strip()[:64]
    page = str(body.get("page") or "").strip()[:300]
    rows = body.get("rows")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        rows = None
    summary = (f"Exported '{filename or 'a file'}' as {fmt} from the browser"
               + (f" ({rows} row{'s' if rows != 1 else ''})" if rows is not None else ""))
    try:
        row = record_audit_strict(
            EXPORT, request=request, target_type=target_type, target_id=target_id,
            summary=summary,
            detail={"channel": "browser", "format": fmt, "filename": filename,
                    "page": page, "rows": rows})
    except AuditWriteError:
        return Response({"detail": "Export not audited; nothing was exported."}, status=503)
    return Response({"audit_id": row.id}, status=201)
