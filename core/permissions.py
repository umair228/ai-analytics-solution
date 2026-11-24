"""Role-based access-control permission classes for the DIS API."""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from core.access import can_edit, can_view


class IsAdmin(BasePermission):
    """Only platform administrators."""

    message = "Administrator role required."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_admin)


class IsAnalystOrAbove(BasePermission):
    """Read for any authenticated user; write requires analyst or admin."""

    message = "Analyst role required to modify this resource."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return user.can_build


class IsOwnerOrSharedReadOnly(BasePermission):
    """Object-level: owner has full access, anyone who can *see* it gets read-only,
    admins always have full access.

    "Can see it" is :func:`core.access.can_view` — the same rule the list querysets use,
    not a second implementation of it. When those two are written separately they drift,
    and the one that drifts silently is the list, which is the one nobody thinks to test.
    """

    message = "You do not have access to this resource."

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if can_edit(obj, user):
            return True
        return request.method in SAFE_METHODS and can_view(obj, user)
