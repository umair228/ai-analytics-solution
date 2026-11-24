"""MFA endpoints.

Who may do what, and the one rule that runs through all of it: **proving you have the second
factor, and removing the second factor, both cost you something.**

  GET    /api/auth/mfa/           is it on?
  POST   /api/auth/mfa/setup/     a fresh secret + QR. Changes nothing yet.
  POST   /api/auth/mfa/confirm/   prove a code → it turns on, and you get the backup codes ONCE
  POST   /api/auth/mfa/disable/   requires the PASSWORD again
  POST   /api/auth/mfa/verify/    the login challenge — unauthenticated, throttled
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import (
    api_view, permission_classes, throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from core.audit import client_ip

from . import lockout, mfa


class MfaVerifyThrottle(ScopedRateThrottle):
    scope = "mfa"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def status_view(request):
    dev = mfa.device_for(request.user)
    return Response({
        "enabled": mfa.is_active(request.user),
        "required": mfa.required_for(request.user),
        "backup_codes_remaining": len(dev.backup_code_hashes) if dev else 0,
        "confirmed_at": dev.confirmed_at if dev else None,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def setup(request):
    """A new secret and a QR. Deliberately inert: nothing about the user's login changes
    until they prove a code, so closing the tab half-way cannot lock them out."""
    return Response(mfa.begin_enrolment(request.user, ip=client_ip(request)))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def confirm(request):
    """Prove the authenticator works → MFA turns on, and the backup codes are shown ONCE."""
    codes = mfa.confirm_enrolment(request.user, request.data.get("code") or "",
                                  ip=client_ip(request))
    if codes is None:
        return Response(
            {"error": True, "detail": "That code is not right. Check the time on your "
                                      "phone — TOTP is a clock, and a phone that has "
                                      "drifted will produce codes we cannot match."},
            status=status.HTTP_400_BAD_REQUEST)

    return Response({
        "enabled": True,
        "backup_codes": codes,
        "warning": ("Write these down now. Each works once, and they are the only way back "
                    "in if you lose the phone. They cannot be shown again — we store only "
                    "their hashes."),
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def disable(request):
    """Turning MFA off requires the password again.

    A second factor you can remove from an already-open session — a borrowed laptop, an
    unlocked screen — is not a second factor, it is a setting."""
    password = request.data.get("password") or ""
    if not request.user.check_password(password):
        return Response({"error": True, "detail": "Wrong password."},
                        status=status.HTTP_403_FORBIDDEN)

    mfa.disable(request.user, by=request.user, ip=client_ip(request))
    return Response({"enabled": False})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([MfaVerifyThrottle])
def verify(request):
    """The second half of the login.  Body: {mfa_token, code}

    ``mfa_token`` is the short-lived signed blob the password step handed back. It is not a
    JWT and nothing else in the system will accept it — if it were usable as a session, the
    second factor would be optional.
    """
    ip = client_ip(request)
    user = mfa.resolve_challenge(request.data.get("mfa_token") or "")
    if user is None:
        # Same shape as any other login failure. "Your challenge expired" is a small
        # kindness that also tells an attacker their guess at the token was well-formed.
        return Response({"detail": "No active account found with the given credentials."},
                        status=status.HTTP_401_UNAUTHORIZED)

    if lockout.check_locked(user.username, ip):
        return Response({"detail": "No active account found with the given credentials."},
                        status=status.HTTP_401_UNAUTHORIZED)

    tokens = mfa.verify_challenge(user, request.data.get("code") or "", ip=ip)
    if tokens is None:
        return Response({"detail": "No active account found with the given credentials."},
                        status=status.HTTP_401_UNAUTHORIZED)

    from .serializers import UserSerializer

    return Response({**tokens, "user": UserSerializer(user).data})
