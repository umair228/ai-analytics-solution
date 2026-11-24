"""The second factor — enrolment, the login challenge, and enforcement.

Part 11 §11.200(a)(1) asks an electronic signature to use *two distinct identification
components*, and DIS already does that: an identification code and a password, re-entered at
the point of signing. So MFA is not what makes us compliant. **MFA is what makes the first of
those two components worth anything after a password has been phished, reused, or read out of
somebody else's breach dump** — and a laboratory's analysts reuse passwords like everybody
else.

The design decisions that matter are in ``accounts/models.py::MfaDevice`` (the secret is
encrypted and not hashed, enrolment must be proved, a code cannot be replayed, backup codes
are single-use). What lives here is the flow, and its own trap:

**The challenge token must not be a session.** After the password check succeeds, the server
has to hand the client *something* that says "this person got the password right, they now owe
me a code". If that something is a JWT, MFA is decorative — you can skip the second step and
use it. So it is a short-lived signed blob under its own salt, it carries nothing but the user
id, and no code path anywhere else will accept it.

**A failed code has to cost something.** A six-digit code is a million guesses, and a login
endpoint that will take a million guesses is not a second factor. Failures feed the same
lockout counter as a failed password (``accounts/lockout.py``), and the endpoint is
rate-limited.
"""
from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from core.audit import log_audit
from core.models import AuditLog

from . import lockout, totp
from .models import MfaDevice

# Its own salt, so a blob minted here cannot be mistaken for — or used as — anything else.
_SALT = "dis.mfa.challenge"
_CHALLENGE_TTL = 300          # five minutes to fetch a code from your phone is plenty


def issue_challenge(user) -> str:
    """"You have proved the password. Now prove the second factor."

    Deliberately NOT a JWT: if this were usable as an access token, the second factor would
    be a formality you could decline.
    """
    return signing.dumps({"uid": user.pk}, salt=_SALT)


def resolve_challenge(token: str):
    """The user this challenge belongs to, or None if it is forged, tampered or stale."""
    from django.contrib.auth import get_user_model

    try:
        data = signing.loads(token or "", salt=_SALT, max_age=_CHALLENGE_TTL)
    except signing.BadSignature:
        return None
    return get_user_model().objects.filter(pk=data.get("uid"), is_active=True).first()


def device_for(user) -> MfaDevice | None:
    return getattr(user, "mfa", None)


def is_active(user) -> bool:
    dev = device_for(user)
    return bool(dev and dev.is_active)


def required_for(user) -> bool:
    """Does this user's role oblige them to have a second factor?

    Empty by default — turning it on for a role that has not enrolled yet must not lock
    anybody out, and it does not: ``accounts.middleware.MfaEnforcementMiddleware`` refuses
    every endpoint EXCEPT the ones that let them enrol.
    """
    roles = getattr(settings, "DSE_REQUIRE_MFA_ROLES", []) or []
    return bool(roles) and getattr(user, "role", None) in roles


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------
def begin_enrolment(user, ip=None) -> dict:
    """A fresh, UNCONFIRMED secret. It gates nothing until a code has been proved."""
    dev, _ = MfaDevice.objects.get_or_create(user=user)
    dev.secret = totp.new_secret()
    dev.confirmed_at = None          # re-enrolling starts over, deliberately
    dev.last_step = None
    dev.backup_code_hashes = []
    dev.save(update_fields=["secret_encrypted", "confirmed_at", "last_step",
                            "backup_code_hashes", "updated_at"])

    log_audit(action=AuditLog.Action.UPDATE, user=user, target_type="accounts.MfaDevice",
              target_id=dev.pk, summary=f"Started MFA enrolment for '{user.username}'",
              ip_address=ip)

    uri = totp.provisioning_uri(dev.secret, account=user.username,
                                issuer=getattr(settings, "DSE_MFA_ISSUER", "DIS"))
    return {
        "secret": dev.secret,        # for manual entry — the only time it leaves the server
        "otpauth_uri": uri,
        "qr_svg": totp.qr_svg(uri),
        "detail": ("Scan this with an authenticator app, then send a code to confirm. "
                   "Nothing changes about your login until you do."),
    }


def confirm_enrolment(user, code: str, ip=None) -> list[str] | None:
    """Prove the app works, then — and only then — turn it on.

    Returns the backup codes, once. None if the code was wrong.
    """
    dev = device_for(user)
    if dev is None or not dev.secret_encrypted:
        return None
    if totp.verify(dev.secret, code) is None:
        log_audit(action=AuditLog.Action.DENIED, user=user,
                  target_type="accounts.MfaDevice", target_id=dev.pk,
                  summary=f"MFA enrolment code rejected for '{user.username}'",
                  ip_address=ip)
        return None

    dev.confirmed_at = timezone.now()
    dev.last_step = totp.step_at()
    dev.save(update_fields=["confirmed_at", "last_step", "updated_at"])
    codes = dev.issue_backup_codes()

    log_audit(action=AuditLog.Action.UPDATE, user=user, target_type="accounts.MfaDevice",
              target_id=dev.pk,
              summary=f"MFA enabled for '{user.username}' ({len(codes)} backup codes issued)",
              ip_address=ip)
    return codes


def disable(user, *, by=None, ip=None) -> None:
    """Removing a second factor is a security decision, so it is audited and it names who.

    The *view* requires the password again before calling this. A second factor you can take
    off without proving who you are is not a second factor — it is a setting.
    """
    dev = device_for(user)
    if dev is None:
        return
    dev.delete()
    log_audit(action=AuditLog.Action.DELETE, user=by or user,
              target_type="accounts.MfaDevice", target_id=user.pk,
              summary=f"MFA DISABLED for '{user.username}'"
                      + (f" by {by.username}" if by and by != user else ""),
              ip_address=ip)


# ---------------------------------------------------------------------------
# The login challenge
# ---------------------------------------------------------------------------
def verify_challenge(user, code: str, ip=None) -> dict | None:
    """A TOTP code, or a backup code. Returns the JWT pair, or None.

    A failure feeds the same lockout counter as a wrong password — otherwise the second
    factor is a six-digit oracle you can query a million times.
    """
    dev = device_for(user)
    if dev is None or not dev.is_active:
        return None

    used_backup = False
    ok = dev.verify(code)
    if not ok:
        ok = used_backup = dev.consume_backup_code(code)

    if not ok:
        lockout.record_failure(user.username, ip)
        log_audit(action=AuditLog.Action.LOGIN_FAILED, user=user, target_type="User",
                  target_id=user.pk,
                  summary=f"MFA code rejected for '{user.username}'",
                  detail={"factor": "mfa"}, ip_address=ip)
        return None

    lockout.record_success(user, ip)
    if used_backup:
        # Worth its own entry: a backup code means the authenticator was unavailable, and a
        # run of them means somebody has lost a phone — or is working around one.
        left = len(dev.backup_code_hashes)
        log_audit(action=AuditLog.Action.LOGIN, user=user, target_type="User",
                  target_id=user.pk,
                  summary=f"'{user.username}' logged in with a BACKUP CODE ({left} left)",
                  detail={"backup_codes_remaining": left}, ip_address=ip)

    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}
