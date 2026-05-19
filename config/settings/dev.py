"""Development settings — SQLite metadata DB, permissive for local work."""
from .base import *  # noqa: F401,F403

DEBUG = True

# Loosen CORS in dev only so any localhost port works while iterating.
CORS_ALLOW_ALL_ORIGINS = True

# Print emails to the console in dev.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
