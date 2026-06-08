"""Optional OIDC single-sign-on wiring (mozilla-django-oidc).

No-op unless ``OIDC_ENABLED=True`` so ``manage.py check`` stays green and the
dependency is only required when SSO is actually switched on. Call
``configure_sso(globals())`` at the end of a settings module (see prod.py).

Endpoints come from explicit ``OIDC_OP_*_ENDPOINT`` env vars when provided;
otherwise they are derived from ``OIDC_OP_BASE_URL`` using the standard
Keycloak ``/protocol/openid-connect/*`` layout. Okta / Auth0 / Azure AD use
different paths — set the explicit endpoints for those providers.
"""
from decouple import Csv, config


def configure_sso(ns):
    if not config("OIDC_ENABLED", default=False, cast=bool):
        return

    base = config("OIDC_OP_BASE_URL", default="").rstrip("/")

    def endpoint(name, suffix):
        explicit = config(f"OIDC_OP_{name}_ENDPOINT", default="")
        if explicit:
            return explicit
        if not base:
            raise RuntimeError(
                "OIDC_ENABLED=True requires OIDC_OP_BASE_URL (or an explicit "
                f"OIDC_OP_{name}_ENDPOINT)."
            )
        return f"{base}{suffix}"

    ns["OIDC_ENABLED"] = True  # consumed by config/urls.py to mount the routes
    ns["INSTALLED_APPS"] = ns["INSTALLED_APPS"] + ["mozilla_django_oidc"]
    # SessionRefresh transparently re-checks the IdP session; it must come after
    # django's AuthenticationMiddleware (already present in base MIDDLEWARE).
    ns["MIDDLEWARE"] = ns["MIDDLEWARE"] + [
        "mozilla_django_oidc.middleware.SessionRefresh",
    ]
    ns["AUTHENTICATION_BACKENDS"] = [
        "config.oidc_backend.LabOIDCBackend",
        "django.contrib.auth.backends.ModelBackend",  # local admin fallback
    ]

    ns["OIDC_RP_CLIENT_ID"] = config("OIDC_RP_CLIENT_ID")
    ns["OIDC_RP_CLIENT_SECRET"] = config("OIDC_RP_CLIENT_SECRET")
    ns["OIDC_RP_SIGN_ALGO"] = config("OIDC_RP_SIGN_ALGO", default="RS256")
    ns["OIDC_RP_SCOPES"] = config("OIDC_RP_SCOPES", default="openid email profile")

    ns["OIDC_OP_AUTHORIZATION_ENDPOINT"] = endpoint("AUTHORIZATION", "/protocol/openid-connect/auth")
    ns["OIDC_OP_TOKEN_ENDPOINT"] = endpoint("TOKEN", "/protocol/openid-connect/token")
    ns["OIDC_OP_USER_ENDPOINT"] = endpoint("USER", "/protocol/openid-connect/userinfo")
    ns["OIDC_OP_JWKS_ENDPOINT"] = endpoint("JWKS", "/protocol/openid-connect/certs")
    # Used for RP-initiated logout (see config.oidc_backend.provider_logout).
    ns["OIDC_OP_LOGOUT_ENDPOINT"] = config("OIDC_OP_LOGOUT_ENDPOINT", default="")

    ns["OIDC_STORE_ID_TOKEN"] = True  # required for id_token_hint on logout
    ns["LOGIN_REDIRECT_URL"] = config("OIDC_LOGIN_REDIRECT", default="/")
    ns["LOGOUT_REDIRECT_URL"] = config("OIDC_LOGOUT_REDIRECT", default="/")
    ns["OIDC_OP_LOGOUT_URL_METHOD"] = "config.oidc_backend.provider_logout"

    # Claim → role mapping. IdP groups are mirrored into Django groups; the
    # listed groups additionally grant staff / superuser.
    ns["OIDC_GROUPS_CLAIM"] = config("OIDC_GROUPS_CLAIM", default="groups")
    ns["OIDC_STAFF_GROUPS"] = config("OIDC_STAFF_GROUPS", default="lab-admins", cast=Csv())
    ns["OIDC_SUPERUSER_GROUPS"] = config("OIDC_SUPERUSER_GROUPS", default="", cast=Csv())
