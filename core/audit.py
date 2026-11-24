"""Lightweight audit-trail helpers used across the API."""
import logging

from django.db import transaction

from .models import AuditLog

logger = logging.getLogger(__name__)

# Actions for data leaving the platform. They are deliberately NOT (yet) members
# of AuditLog.Action: adding choices is a schema migration, and Django does not
# enforce choices at the database level, so these values store and filter
# (?action=EXPORT) like any other. When the choices are added later, rows
# written now already carry the right value.
EXPORT = "EXPORT"   # a file downloaded from the API
SEND = "SEND"       # a file emailed by a scheduled report


class AuditWriteError(Exception):
    """A fail-closed audit row could not be committed."""


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


def record_audit_strict(action, *, user=None, request=None, target_type="",
                        target_id="", summary="", detail=None) -> AuditLog:
    """Commit an audit row or raise :class:`AuditWriteError` — never swallow.

    For actions that must not happen unaudited (exports, emailed reports): the
    caller writes this row first and only releases the data once it returns.
    The row is committed in its own atomic block, so a failure (database down,
    constraint, disk full) surfaces here, before any data leaves.
    """
    if request is not None:
        if user is None:
            user = getattr(request, "user", None)
        ip = client_ip(request)
    else:
        ip = None
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    try:
        with transaction.atomic():
            return AuditLog.objects.create(
                user=user,
                action=action,
                target_type=target_type,
                target_id=str(target_id or ""),
                summary=(summary or "")[:255],
                detail=detail or {},
                ip_address=ip,
            )
    except Exception as exc:  # noqa: BLE001 — re-raised as one typed error
        logger.error("Audit write failed (%s %s#%s): %s",
                     action, target_type, target_id, exc)
        raise AuditWriteError(str(exc)) from exc
