"""Production settings — PostgreSQL metadata DB, security hardened."""
from decouple import config

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

# Security hardening
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = config("DJANGO_SSL_REDIRECT", default=True, cast=bool)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"

# Use SMTP in prod when EMAIL_HOST is configured, fall back to console.
if config("EMAIL_HOST", default=""):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
