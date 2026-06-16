"""Which LIMS tables to mirror, and how.

Defaults cover the EGPC transactional core (SAMPLE / RESULT / TEST), all of which
carry a CHANGED_ON modification timestamp → incremental refresh keyed on it, with
a periodic full pass to catch hard-deletes. Reference tables (small, rarely
changed) are full-only. Override per deployment via
``settings.DSE_REPLICATION_TABLES`` (a list of the same dicts).
"""
from django.conf import settings

# Status code → label, mirrored from seed_egpc_demo.STATUS_CASE so the mart's
# status_label matches the curated dashboards exactly.
STATUS_LABELS = {
    "A": "Authorized", "C": "Complete", "X": "Cancelled", "R": "Rejected",
    "U": "Unreceived", "I": "In Progress", "P": "Pending", "D": "Disposed",
}

_DEFAULT_TABLES = [
    {"table": "SAMPLE", "keys": ["SAMPLE_NUMBER"],
     "watermark": "CHANGED_ON", "cadence": "hourly"},
    {"table": "RESULT", "keys": ["SAMPLE_NUMBER", "RESULT_NUMBER"],
     "watermark": "CHANGED_ON", "cadence": "hourly"},
    {"table": "TEST", "keys": ["SAMPLE_NUMBER", "TEST_NUMBER"],
     "watermark": "CHANGED_ON", "cadence": "hourly"},
    # Reference data — full copy, no watermark. Skipped silently if absent.
    {"table": "PRODUCT_SPEC", "keys": [], "watermark": None, "cadence": "weekly"},
    {"table": "LIMS_USERS", "keys": [], "watermark": None, "cadence": "weekly"},
    {"table": "INSTRUMENTS", "keys": [], "watermark": None, "cadence": "weekly"},
]


def table_specs() -> list[dict]:
    """The configured table specs (settings override or the EGPC defaults)."""
    return list(getattr(settings, "DSE_REPLICATION_TABLES", None) or _DEFAULT_TABLES)
