"""Role-based access-control permission classes for the DSE API."""
from rest_framework.permissions import SAFE_METHODS, BasePermission


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
    """Object-level: owner has full access, shared users get read-only,
    admins always have full access."""

    message = "You do not have access to this resource."

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_admin:
            return True
        if getattr(obj, "owner_id", None) == user.id:
            return True
        if request.method in SAFE_METHODS:
            shared = getattr(obj, "shared_with", None)
            if shared is not None and shared.filter(pk=user.pk).exists():
                return True
        return False
