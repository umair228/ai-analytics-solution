"""Detect and report attempts at unauthorised use — 21 CFR Part 11 §11.300(d).

    "Use of transaction safeguards to prevent unauthorized use of passwords and/or
     identification codes, and to detect and report in an immediate and urgent manner
     any attempts at their unauthorized use to the system security unit…"

None of that existed. ``LoginView`` audited a login only ``if response.status_code == 200``
— so a **failed** login produced no entry anywhere, and the audit trail could not tell a
quiet night from a night spent guessing passwords. There was no lockout, no throttle, and
nothing to report.

Three things happen here, and the order matters:

1. **Every attempt is recorded**, success or failure, with the username tried and the
   address it came from. A failure against an account that does not exist is recorded too —
   that is what username enumeration looks like, and it is the attempt, not the account,
   that is evidence.
2. **The account locks** after ``DSE_LOGIN_MAX_FAILURES`` consecutive failures, for
   ``DSE_LOGIN_LOCKOUT_MINUTES``. The lock **expires**; it does not need an administrator
   at three in the morning.
3. **The lockout is reported urgently**, because §11.300(d) asks for reporting, not merely
   recording. A line in a table nobody opens is not a report.

**What is deliberately NOT done:** the login response never says whether the *username*
existed, whether the *password* was wrong, or whether the account is locked-versus-absent.
Every failure returns the same thing. Telling an attacker "that account exists but is
locked" hands them a working username and a denial-of-service in one sentence.

**The trade being made:** locking by username means someone who knows a name can lock that
person out. For an on-prem laboratory of named analysts behind a firewall this is worth it,
and the expiring lock bounds the damage. It would be the wrong trade for a public consumer
service.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.audit import log_audit
from core.models import AuditLog

logger = logging.getLogger(__name__)


def _max_failures() -> int:
    return int(getattr(settings, "DSE_LOGIN_MAX_FAILURES", 5) or 5)


def _lockout_minutes() -> int:
    return int(getattr(settings, "DSE_LOGIN_LOCKOUT_MINUTES", 15) or 15)


def _find(username: str):
    if not username:
        return None
    return get_user_model().objects.filter(username=username).first()


def record_failure(username: str, ip: str | None) -> None:
    """A failed attempt: audit it, count it, and lock the account if it is time.

    Note the entry is written even when no such user exists. A burst of failures against
    names that are not accounts is exactly what enumeration looks like, and an audit trail
    that only records attempts on *real* accounts cannot show it.
    """
    user = _find(username)

    log_audit(
        action=AuditLog.Action.LOGIN_FAILED,
        user=user,
        target_type="User",
        target_id=getattr(user, "pk", "") or "",
        summary=f"Failed login for '{username[:60]}'",
        detail={"username": username[:150], "account_exists": user is not None},
        ip_address=ip,
    )

    if user is None:
        return

    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= _max_failures() and not user.is_locked:
        minutes = _lockout_minutes()
        user.locked_until = timezone.now() + timezone.timedelta(minutes=minutes)
        user.save(update_fields=["failed_login_count", "locked_until"])
        _report_lockout(user, ip, minutes)
    else:
        user.save(update_fields=["failed_login_count"])


def record_success(user, ip: str | None) -> None:
    """A good login clears the counter — the threshold is *consecutive* failures.

    Without this, five typos spread over a year would lock an account, and the lockout
    would be measuring forgetfulness rather than attack.
    """
    if user.failed_login_count or user.locked_until:
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=["failed_login_count", "locked_until"])

    log_audit(action=AuditLog.Action.LOGIN, user=user, target_type="User",
              target_id=user.pk, summary=f"Login: {user.username}", ip_address=ip)


def check_locked(username: str, ip: str | None):
    """Return the locked user, if this username is currently locked out.

    The caller must refuse the login WITHOUT saying why — see the module docstring.
    """
    user = _find(username)
    if user is not None and user.is_locked:
        log_audit(
            action=AuditLog.Action.DENIED, user=user, target_type="User",
            target_id=user.pk,
            summary=f"Login refused — '{user.username}' is locked out",
            detail={"locked_until": user.locked_until.isoformat()},
            ip_address=ip,
        )
        return user
    return None


def unlock(user, *, by=None, ip=None) -> None:
    """Administrative unlock — itself an audited act, because it is a security decision."""
    user.failed_login_count = 0
    user.locked_until = None
    user.save(update_fields=["failed_login_count", "locked_until"])
    log_audit(action=AuditLog.Action.UNLOCK, user=by, target_type="User",
              target_id=user.pk, summary=f"Unlocked '{user.username}'", ip_address=ip)


def _report_lockout(user, ip, minutes: int) -> None:
    """§11.300(d) asks for reporting "in an immediate and urgent manner". A row in a table
    nobody opens is not a report — so this also goes out as mail, and it must never be the
    reason a login request 500s."""
    log_audit(
        action=AuditLog.Action.LOCKOUT, user=user, target_type="User", target_id=user.pk,
        summary=f"'{user.username}' locked for {minutes} min after "
                f"{user.failed_login_count} consecutive failed logins",
        detail={"failures": user.failed_login_count, "minutes": minutes,
                "locked_until": user.locked_until.isoformat()},
        ip_address=ip,
    )
    logger.warning("SECURITY: account '%s' locked after %d failed logins from %s",
                   user.username, user.failed_login_count, ip or "an unknown address")

    recipients = _security_recipients()
    if not recipients:
        return
    try:
        from django.core.mail import send_mail

        send_mail(
            subject=f"[DIS] SECURITY — account '{user.username}' locked out",
            message=(
                f"The account '{user.username}' was locked for {minutes} minutes after "
                f"{user.failed_login_count} consecutive failed login attempts.\n\n"
                f"Most recent attempt came from: {ip or 'an unknown address'}\n"
                f"Locked until: {user.locked_until:%Y-%m-%d %H:%M:%S} UTC\n\n"
                f"If this was not the account holder, treat it as an attempt at "
                f"unauthorised use and investigate the source address.\n\n"
                f"The full trail is in Audit → filter on LOGIN_FAILED."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=recipients,
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001
        # The report is best-effort; the audit entry above is the record of account. A mail
        # server that is down must not become a way to prevent lockouts from happening.
        logger.exception("Could not send the lockout notification for '%s'", user.username)


def _security_recipients() -> list[str]:
    explicit = getattr(settings, "DSE_SECURITY_EMAILS", None)
    if explicit:
        return list(explicit)
    User = get_user_model()
    return list(
        User.objects.filter(is_active=True, role=User.Role.ADMIN)
        .exclude(email="").values_list("email", flat=True)
    )
