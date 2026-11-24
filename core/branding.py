"""Deployment branding, read from settings (DIS_BRAND_*, DIS_PROFILE).

``branding()`` is the configured brand exactly as set — blank means "not set",
and the SPA resolves blanks to its build-time / built-in names. Server-side
files and emails need a concrete name: :func:`product_name` falls back to the
lab wording, so a deployment that configures no brand keeps the exports and
emails it had before branding existed.
"""
from django.conf import settings

# The wording the lab deployment's exports and emails have always used.
LAB_PRODUCT_NAME = "Data Science Engine"


def branding() -> dict:
    return {
        "product_name": settings.DIS_BRAND_PRODUCT_NAME,
        "short_name": settings.DIS_BRAND_SHORT_NAME,
        "tagline": settings.DIS_BRAND_TAGLINE,
        "footer": settings.DIS_BRAND_FOOTER,
        "profile": settings.DIS_PROFILE,
    }


def product_name() -> str:
    """The product name for report headers and titles (never blank)."""
    return (settings.DIS_BRAND_PRODUCT_NAME or "").strip() or LAB_PRODUCT_NAME


def footer() -> str:
    """The brand footer for reports and emails ("" when not configured)."""
    return (settings.DIS_BRAND_FOOTER or "").strip()
