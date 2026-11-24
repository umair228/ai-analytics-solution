"""Test helpers shared across apps.

``REPORTING_DEFAULTS`` are the documented defaults of the reporting settings
(config/settings/base.py). A deployment's ``.env`` — e.g. a branded reporting
profile on a dev box — is also read by the test runner, so tests that rely on
default behaviour pin these with ``override_settings(**REPORTING_DEFAULTS)``
instead of trusting the environment. ``core.tests`` checks that the values
here still match the real defaults.
"""
REPORTING_DEFAULTS = {
    "DIS_BRAND_PRODUCT_NAME": "",
    "DIS_BRAND_SHORT_NAME": "",
    "DIS_BRAND_TAGLINE": "",
    "DIS_BRAND_FOOTER": "",
    "DIS_PROFILE": "full",
    "DIS_REPORT_TZ": "Asia/Riyadh",
    "DIS_REPORT_SEND_HOUR": None,
    "DIS_REPORT_CALENDAR_MONTHS": False,
    "DIS_EMAIL_SUBJECT_PREFIX": "[DIS Report]",
    "DIS_ALERT_SUBJECT_PREFIX": "[DIS Alert]",
    "DIS_EXPORT_MAX_ROWS": 100000,
    "DIS_EXPORT_PDF_MAX_ROWS": 2000,
}
