"""MFA enforcement, at a layer no view can opt out of.

The first attempt at this was a DRF permission class in ``DEFAULT_PERMISSION_CLASSES``, and
it was **decorative**. Nearly every viewset in this codebase sets its own
``permission_classes`` or overrides ``get_permissions()`` — ``DatasetViewSet`` returns
``[IsAuthenticated()]`` for list and retrieve — and an override *replaces* the defaults rather
than adding to them. So the "global" policy applied to precisely the views that had not
bothered to have an opinion. A required-MFA user sailed straight into ``/api/datasets/``.

That is the whole class of bug this file exists to close: **a control that any file can drop
by accident is not a control**, and there were twenty places to drop this one.

Middleware runs for every request, before the view, and there is nothing a view can write that
turns it off.

Two things it is careful about:

* **It cannot lock anyone out.** The endpoints that let a user *enrol* stay open. Switching
  the policy on for a role that has not enrolled yet means those users sign in, get refused
  everything else, and are walked to the QR code. Refusing the login instead would leave them
  outside the building with the instructions locked inside it.
* **It authenticates the same way DRF does**, because ``request.user`` is anonymous in
  middleware for a JWT request — Django's ``AuthenticationMiddleware`` only knows about
  sessions, and the API is bearer-token. So it asks the same authentication classes.
"""
from __future__ import annotations

import json

from django.http import HttpResponse

# Paths a non-enrolled user must still be able to reach — or the policy would be a trap.
_EXEMPT_PREFIXES = (
    "/api/auth/",        # login, refresh, me, and every mfa/* endpoint
    "/api/health",
    "/admin/",           # an administrator locked out of the admin cannot fix anything
    "/static/",
    "/media/",
)


class MfaEnforcementMiddleware:
    """403 every API call by a user whose role requires MFA and who has not enrolled."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._blocked(request):
            return HttpResponse(
                json.dumps({
                    "detail": ("Your role requires two-factor authentication. Set it up to "
                               "continue — Profile → Security."),
                    "mfa_enrolment_required": True,
                }),
                status=403, content_type="application/json",
            )
        return self.get_response(request)

    def _blocked(self, request) -> bool:
        path = request.path or ""
        if not path.startswith("/api/") or path.startswith(_EXEMPT_PREFIXES):
            return False

        user = self._user(request)
        if user is None or not user.is_authenticated:
            return False          # not our question — authentication answers that one

        from . import mfa

        return mfa.required_for(user) and not mfa.is_active(user)

    @staticmethod
    def _user(request):
        """The user DRF *would* see. `request.user` is anonymous here for a bearer token."""
        from rest_framework.exceptions import APIException
        from rest_framework.request import Request

        from accounts.authentication import APITokenAuthentication

        # A session user (the browsable admin, tests using force_login) is already resolved.
        existing = getattr(request, "user", None)
        if existing is not None and getattr(existing, "is_authenticated", False):
            return existing

        drf_request = Request(request)
        for backend in (_jwt(), APITokenAuthentication()):
            if backend is None:
                continue
            try:
                result = backend.authenticate(drf_request)
            except APIException:
                return None       # a bad credential is not our problem; let DRF reject it
            if result is not None:
                return result[0]
        return None


def _jwt():
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication

        return JWTAuthentication()
    except ImportError:          # pragma: no cover
        return None
