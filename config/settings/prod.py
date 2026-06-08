"""Production settings — PostgreSQL metadata DB, security hardened."""
from decouple import Csv, config

from .base import *  # noqa: F401,F403

DEBUG = False

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("PG_DB_NAME", default="dse"),
        "USER": config("PG_DB_USER", default="dse"),
        "PASSWORD": config("PG_DB_PASSWORD", default=""),
        "HOST": config("PG_DB_HOST", default="127.0.0.1"),
        "PORT": config("PG_DB_PORT", default="5432"),
        "CONN_MAX_AGE": 60,
    }
}

# Security hardening.
# Defaults assume HTTPS. For an HTTP-only pilot (no TLS in front yet) set
# DJANGO_SSL_REDIRECT=False — secure cookies then default off too, otherwise the
# browser won't send session/CSRF cookies over plain HTTP and admin login fails.
# Both flip back on automatically once TLS is configured (set both True).
_SSL = config("DJANGO_SSL_REDIRECT", default=True, cast=bool)
_SECURE_COOKIES = config("DJANGO_SECURE_COOKIES", default=_SSL, cast=bool)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = _SSL
SECURE_HSTS_SECONDS = 31536000 if _SSL else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _SSL
SECURE_HSTS_PRELOAD = _SSL
SESSION_COOKIE_SECURE = _SECURE_COOKIES
CSRF_COOKIE_SECURE = _SECURE_COOKIES
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"
# Needed for Django admin POST/login over the server's bare IP or hostname,
# e.g. CSRF_TRUSTED_ORIGINS=http://20.174.10.31
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# Use SMTP in prod when EMAIL_HOST is configured, fall back to console.
if config("EMAIL_HOST", default=""):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

# Optional OIDC single-sign-on (no-op unless OIDC_ENABLED=True). Kept last so it
# can extend INSTALLED_APPS / MIDDLEWARE / AUTHENTICATION_BACKENDS.
from .sso import configure_sso  # noqa: E402
configure_sso(globals())
