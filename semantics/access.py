"""Clearance for classified numbers (the scope loader's runtime half).

A certified metric or tile inherits the classes of the views it reads (``cost``,
``bid_confidential``, later ``personal``). Seeing one needs clearance for every
class: an administrator, or a member of the Django group configured for the class
in ``DIS_CLASS_CLEARANCE`` (e.g. ``cost=Cost cleared;bid_confidential=Bid confidential``).
A class with no configured group is cleared for administrators only, so a new
class is closed by default rather than open.
"""
from __future__ import annotations

from django.conf import settings


def clearance_groups() -> dict:
    return dict(getattr(settings, "DIS_CLASS_CLEARANCE", None) or {})


def cleared_classes(user) -> frozenset:
    if not (user and getattr(user, "is_authenticated", False)):
        return frozenset()
    from .spec import CLASSES

    if getattr(user, "is_admin", False):
        return frozenset(CLASSES)
    groups = set(user.groups.values_list("name", flat=True))
    return frozenset(cls for cls, group in clearance_groups().items() if group in groups)


def is_cleared(user, classes) -> bool:
    return set(classes or ()) <= cleared_classes(user)
