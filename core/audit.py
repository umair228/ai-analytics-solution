"""Lightweight audit-trail helpers used across the API."""
from .models import AuditLog


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def record_audit(request, action, *, target_type="", target_id="", summary="", detail=None):
    """Persist an audit-log entry. Best-effort: never raises."""
    try:
        user = getattr(request, "user", None)
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None
        AuditLog.objects.create(
            user=user,
            action=action,
            target_type=target_type,
            target_id=str(target_id or ""),
            summary=(summary or "")[:255],
            detail=detail or {},
            ip_address=client_ip(request),
        )
    except Exception:  # noqa: BLE001 - auditing must never break a request
        pass
