"""Audit-trail helpers used across the API.

The audit trail is *evidence*. A write that fails silently is worse than no
audit trail at all, because the resulting gap is indistinguishable from "nothing
happened" — an auditor cannot tell a quiet period from a broken logger. So the
writers here **fail closed**: if the entry cannot be persisted, the caller's
request fails with it.

That is only a safe posture because the inputs are sanitised below. Oversized
fields, a non-serialisable ``detail``, and an attacker-controlled
``X-Forwarded-For`` are the failure modes that made the old blanket
``except: pass`` look necessary; each is now coerced rather than raised on, so
the only errors that reach the caller are real ones (e.g. the database is down,
in which case the request was failing anyway).

``AUDIT_FAIL_OPEN=True`` restores the old best-effort behaviour. It voids the
ALCOA+ / Part 11 attributability claim in SECURITY.md and exists only as an
incident escape hatch.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging

from django.conf import settings

from .models import AuditLog

logger = logging.getLogger(__name__)

_MAX_TARGET_TYPE = 80
_MAX_TARGET_ID = 80
_MAX_SUMMARY = 255
_MAX_REASON = 500


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _safe_ip(value):
    """Coerce an unparseable address to NULL.

    ``AuditLog.ip_address`` is a GenericIPAddressField, which the database
    rejects junk for — and ``X-Forwarded-For`` is set by whatever is upstream of
    us. A malformed header must not be able to suppress an audit entry.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return None
    return text


def _safe_detail(detail):
    """Guarantee the JSONField payload actually serialises.

    Round-tripping through ``json`` reduces the value to plain types, so a stray
    Decimal/datetime/model instance degrades to its string form instead of
    blowing up the write.
    """
    if not detail:
        return {}
    try:
        return json.loads(json.dumps(detail, default=str))
    except (TypeError, ValueError):
        return {"unserializable": repr(detail)[:2000]}


# ---------------------------------------------------------------------------
# What a record USED to say — 21 CFR Part 11 §11.10(e)
#
#   "…record changes shall not obscure previously recorded information."
#
# The trail said "Updated data source 'EGPC LIMS'" and stopped. An assessor asks what it
# said before, and there is no answer anywhere in the system.
#
# The obvious implementation is the dangerous one. A diff over model_to_dict() would put
# `DataSource.password` — and the encrypted credential blob, and Dataset.cached_rows,
# which is laboratory data — into AuditLog.detail, which is served by a REST endpoint and
# copied into every nightly backup. A compliance feature that creates a credential leak is
# not a compliance feature. So: redact by name, skip by name, and truncate everything.
# ---------------------------------------------------------------------------
_REDACT = ("password", "secret", "token", "credential", "api_key", "apikey",
           "private_key", "passphrase", "encrypted")

# Big, noisy, or derived — auditing them tells you nothing and costs a lot. cached_rows in
# particular is the query's *result set*: laboratory data, which does not belong in a trail
# that records who changed the configuration.
_SKIP = ("cached_rows", "cached_columns", "row_count", "total_row_count",
         "last_refreshed_at", "last_error", "last_run_at", "last_used_at",
         "last_tested_at", "next_refresh_at", "created_at", "updated_at",
         "test_status", "last_test_error")

_MAX_VALUE = 500


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _REDACT)


def _opaque(value) -> str:
    """A marker that hides a secret but still *changes when the secret changes*.

    The obvious redaction — replace every secret with a constant ``"***"`` — destroys the
    one thing the trail is here to record. Both sides of the diff become ``"***"``, they
    compare equal, and **a password change leaves no entry at all.** The redaction would
    have erased the very fact it existed to capture.

    So: a keyed MAC, truncated. Equal secrets produce equal markers (no phantom change);
    different secrets produce different markers (the change is recorded). It is a MAC and
    not a bare hash on purpose — an attacker holding the audit table, or a copy of the
    nightly dump, cannot grind it offline without ``SECRET_KEY``, which is not in the
    database.
    """
    if value in (None, ""):
        return None
    key = str(getattr(settings, "SECRET_KEY", "")).encode("utf-8")
    mac = hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:12]
    return f"***({mac})"


def _clean(name, value):
    """One field's value, made safe to store and to serve."""
    if _is_secret(name):
        # Record THAT it changed — never what to. "The password was changed" is the
        # audit-relevant fact; the password is not.
        return _opaque(value)
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > _MAX_VALUE:
        return f"{text[:_MAX_VALUE]}… ({len(text)} chars)"
    return value if not isinstance(value, str) or len(value) <= _MAX_VALUE else text


def snapshot(obj, fields=None) -> dict:
    """The auditable state of a model instance: concrete, editable, non-secret fields.

    Call it BEFORE you save, keep the result, and pass it to :func:`record_change`.
    """
    if obj is None:
        return {}
    out = {}
    for field in obj._meta.concrete_fields:
        name = field.name
        if name in ("id", "pk") or name in _SKIP:
            continue
        if fields is not None and name not in fields:
            continue
        if not getattr(field, "editable", True):
            continue
        try:
            value = getattr(obj, field.attname, None)
        except Exception:          # a deferred or broken descriptor must not kill the write
            continue
        out[name] = _clean(name, value)
    return out


def diff(before: dict, after: dict) -> dict:
    """``{field: {"from": …, "to": …}}`` for the fields that actually changed."""
    changed = {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old != new:
            changed[key] = {"from": old, "to": new}
    return changed


def change_reason(request) -> str:
    """The stated reason for a change, if the caller gave one.

    Accepted in the body as `reason`, or in an `X-Change-Reason` header so that a PATCH
    body does not have to carry a field the serializer would reject.
    """
    data = getattr(request, "data", None) or {}
    reason = ""
    if hasattr(data, "get"):
        reason = data.get("reason") or data.get("change_reason") or ""
    return str(reason or request.META.get("HTTP_X_CHANGE_REASON", "") or "").strip()


def require_reason(request, target_type: str) -> None:
    """Reject a change to a controlled record type that arrives without a reason.

    Off unless DSE_REQUIRE_CHANGE_REASON names the type — enabling it rejects updates that
    older clients still send without one, so it must be a deliberate act, not a surprise.
    """
    controlled = getattr(settings, "DSE_REQUIRE_CHANGE_REASON", []) or []
    if target_type in controlled and not change_reason(request):
        from rest_framework.exceptions import ValidationError
        raise ValidationError({
            "reason": f"A reason is required to change a {target_type}. "
                      f"Send `reason` in the body or an X-Change-Reason header."})


def record_change(request, action, obj, *, before=None, target_type=None, summary=""):
    """Audit a create/update/delete **with what actually changed**."""
    target_type = target_type or type(obj).__name__
    after = {} if action == AuditLog.Action.DELETE else snapshot(obj)
    before = before or ({} if action == AuditLog.Action.CREATE else snapshot(obj))

    detail = {}
    if action == AuditLog.Action.UPDATE:
        changes = diff(before, after)
        if not changes:
            return None          # nothing changed; an entry saying so is noise, not evidence
        detail["changes"] = changes
    elif action == AuditLog.Action.CREATE:
        detail["created"] = after
    elif action == AuditLog.Action.DELETE:
        # The whole record, because after this there is nowhere else to read it.
        detail["deleted"] = before

    return log_audit(
        action=action,
        user=getattr(request, "user", None),
        target_type=target_type,
        target_id=getattr(obj, "pk", "") or "",
        summary=summary or f"{action.title()} {target_type} '{obj}'"[:_MAX_SUMMARY],
        detail=detail,
        reason=change_reason(request),
        ip_address=client_ip(request),
    )


def log_audit(*, action, user=None, target_type="", target_id="", summary="",
              detail=None, reason="", ip_address=None):
    """Write one audit entry from an explicit user — no request required.

    The agent/tool layer holds a user but no request, and its actions (an LLM
    composing SQL against the LIMS) are precisely the ones that most need a log.
    """
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    try:
        return AuditLog.objects.create(
            user=user,
            action=action,
            target_type=str(target_type or "")[:_MAX_TARGET_TYPE],
            target_id=str(target_id or "")[:_MAX_TARGET_ID],
            summary=str(summary or "")[:_MAX_SUMMARY],
            detail=_safe_detail(detail),
            reason=str(reason or "")[:_MAX_REASON],
            ip_address=_safe_ip(ip_address),
        )
    except Exception:
        logger.exception(
            "AUDIT WRITE FAILED — action=%s target=%s/%s user=%s",
            action, target_type, target_id, getattr(user, "pk", None),
        )
        if getattr(settings, "AUDIT_FAIL_OPEN", False):
            return None
        raise


def record_audit(request, action, *, target_type="", target_id="", summary="", detail=None):
    """Request-flavoured wrapper around :func:`log_audit`."""
    return log_audit(
        action=action,
        user=getattr(request, "user", None),
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        detail=detail,
        ip_address=client_ip(request),
    )
