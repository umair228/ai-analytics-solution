"""Which data sources may this user reach?

This function existed four times — identical, copy-pasted into ``semantics/views.py``,
``coa/views.py``, ``replication/views.py`` and ``ai/tools.py``. Four copies of an access
rule is four chances to fix three of them, and the one you miss is the one that leaks.

It lives here now, and it defers to :mod:`core.access` for the actual rule.
"""
from __future__ import annotations

from core.access import visible_to

from .constants import SourceType
from .models import DataSource


def visible_datasources(user, *, databases_only: bool = True):
    """Data sources ``user`` may read. ``databases_only`` drops file/upload sources,
    which is what every caller that queries *live* SQL wants."""
    qs = visible_to(DataSource.objects.all(), user)
    if databases_only:
        qs = qs.filter(source_type__in=list(SourceType.DATABASE_TYPES))
    return qs.order_by("name")
