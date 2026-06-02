"""Canonical refinery-laboratory personnel roster — the single source of truth
shared by the Django user seeder (``accounts`` / ``seed_lab``) and the
operational warehouse builder (``seed_refinery.build_warehouse``).

The 16 bench analysts live in ``reference.json`` (so the warehouse SAMPLE /
test-result data stays byte-identical and deterministic). This module adds the
laboratory **management and support** staff that complete a realistic lab org
chart, and provides the glue both sides need:

  * ``WAREHOUSE_ROLE_TO_DJANGO`` — maps a bench analyst's warehouse role string
    ("Section Head", ...) onto an ``accounts.User.Role`` value.
  * ``SUPPORT_STAFF`` — the management/support people not involved in bench
    sample analysis (lab manager, QA manager, sample custodian, instrument
    engineer, technicians, a read-only client).
  * ``technician_names()`` — the subset of named people who appear in the
    warehouse as instrument operators / inventory handlers / calibrators, so an
    ``INSTRUMENTS1_LOG.ENTERED_BY`` value always refers to a real login account.
  * ``username_for`` / ``email_for`` — deterministic credential helpers.

Keeping one roster means a sample's ANALYST, an instrument log's ENTERED_BY and
a platform login all refer to the same people.
"""
from __future__ import annotations

import re
import unicodedata

EMAIL_DOMAIN = "refinery.demo"

# The 8 canonical lab sections (codes + display names). These names match
# ``build_warehouse.SECTION_NAME`` exactly so a Django Lab and a warehouse
# SECTION line up one-to-one.
SECTION_NAME = {
    "CRUDE_DIST": "Crude & Distillation",
    "MOGAS_CHEM": "Gasoline/Mogas",
    "NAPH_AROM": "Naphtha & Aromatics",
    "MIDDIST": "Middle Distillates",
    "LUBE_ASPHALT": "Fuel Oil & Asphalt",
    "GAS_LPG": "Gas & LPG",
    "ENV_WATER": "Environmental & Water",
    "QC_CAL": "QA/QC & Calibration",
}

# Bench analyst warehouse role -> accounts.User.Role value.
WAREHOUSE_ROLE_TO_DJANGO = {
    "Section Head": "section_head",
    "Senior Analyst": "senior_analyst",
    "Analyst": "analyst",
    "QC Chemist": "qc_chemist",
}

# Management & support staff (NOT in reference.json). Each: name, role,
# section_code (None = laboratory-wide), is_staff (Django admin access),
# in_warehouse (whether they show up as a warehouse technician/operator).
SUPPORT_STAFF = [
    {"name": "Sami Al-Harthi",   "role": "lab_manager",         "section_code": None,      "is_staff": True,  "in_warehouse": False},
    {"name": "Layla Al-Mutairi", "role": "qa_manager",          "section_code": "QC_CAL",  "is_staff": True,  "in_warehouse": True},
    {"name": "Hassan Al-Zahid",  "role": "sample_custodian",    "section_code": None,      "is_staff": False, "in_warehouse": True},
    {"name": "Vijay Menon",      "role": "instrument_engineer", "section_code": "QC_CAL",  "is_staff": False, "in_warehouse": True},
    {"name": "Maria Gomez",      "role": "lab_technician",      "section_code": "CRUDE_DIST",   "is_staff": False, "in_warehouse": True},
    {"name": "Daniel Okeke",     "role": "lab_technician",      "section_code": "MIDDIST",      "is_staff": False, "in_warehouse": True},
    {"name": "Priya Sharma",     "role": "lab_technician",      "section_code": "MOGAS_CHEM",   "is_staff": False, "in_warehouse": True},
    {"name": "Karim Haddad",     "role": "lab_technician",      "section_code": "NAPH_AROM",    "is_staff": False, "in_warehouse": True},
    {"name": "Thomas Okafor",    "role": "lab_technician",      "section_code": "GAS_LPG",      "is_staff": False, "in_warehouse": True},
    {"name": "Nadia Al-Sultan",  "role": "viewer",              "section_code": None,           "is_staff": False, "in_warehouse": False},
]


def _ascii(text: str) -> str:
    """Fold accents and drop anything that is not an ASCII letter/space."""
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return norm


def username_for(name: str) -> str:
    """`Abdullah Al-Qahtani` -> `abdullah.alqahtani` (stable, ASCII, lowercase)."""
    parts = _ascii(name).lower().split()
    if len(parts) == 1:
        return re.sub(r"[^a-z0-9]", "", parts[0])
    first = re.sub(r"[^a-z0-9]", "", parts[0])
    last = re.sub(r"[^a-z0-9]", "", "".join(parts[1:]))
    return f"{first}.{last}"


def email_for(username: str) -> str:
    return f"{username}@{EMAIL_DOMAIN}"


def split_name(name: str) -> tuple[str, str]:
    """`Abdullah Al-Qahtani` -> ('Abdullah', 'Al-Qahtani')."""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def technician_names() -> list[str]:
    """Named support staff who appear in the warehouse as instrument operators,
    inventory handlers, calibrators and vision reviewers."""
    return [p["name"] for p in SUPPORT_STAFF if p["in_warehouse"]]
