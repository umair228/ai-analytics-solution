"""Custom OIDC backend: map IdP claims to DIS users, groups and roles.

Imported only when OIDC is enabled (referenced from AUTHENTICATION_BACKENDS in
config.settings.sso), so mozilla-django-oidc stays an optional dependency.
The IdP is the source of truth for group membership and elevated roles.
"""
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.models import Group
from mozilla_django_oidc.auth import OIDCAuthenticationBackend


class LabOIDCBackend(OIDCAuthenticationBackend):
    def _sync_roles(self, user, claims):
        claim = getattr(settings, "OIDC_GROUPS_CLAIM", "groups")
        groups = claims.get(claim) or []
        if isinstance(groups, str):
            groups = [groups]
        groupset = {str(g) for g in groups}

        # Mirror IdP groups into Django groups (IdP = source of truth).
        django_groups = [Group.objects.get_or_create(name=g)[0] for g in groupset]
        user.groups.set(django_groups)

        staff = set(getattr(settings, "OIDC_STAFF_GROUPS", []))
        supers = set(getattr(settings, "OIDC_SUPERUSER_GROUPS", []))
        user.is_superuser = bool(groupset & supers)
        user.is_staff = user.is_superuser or bool(groupset & staff)

    def create_user(self, claims):
        user = super().create_user(claims)
        user.first_name = claims.get("given_name", "")
        user.last_name = claims.get("family_name", "")
        self._sync_roles(user, claims)
        user.save()
        return user

    def update_user(self, user, claims):
        user.first_name = claims.get("given_name", user.first_name)
        user.last_name = claims.get("family_name", user.last_name)
        self._sync_roles(user, claims)
        user.save()
        return user


def provider_logout(request):
    """RP-initiated logout: send the user to the IdP end-session endpoint."""
    base = getattr(settings, "OIDC_OP_LOGOUT_ENDPOINT", "")
    if not base:
        token_ep = getattr(settings, "OIDC_OP_TOKEN_ENDPOINT", "")
        base = token_ep.replace("/token", "/logout") if token_ep else ""
    redirect = request.build_absolute_uri(settings.LOGOUT_REDIRECT_URL)
    if not base:
        return redirect
    params = {"post_logout_redirect_uri": redirect}
    id_token = request.session.get("oidc_id_token")
    if id_token:
        params["id_token_hint"] = id_token
    return f"{base}?{urlencode(params)}"
