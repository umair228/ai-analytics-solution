"""The one definition of "may this user see this object?".

There was no such definition. There were fourteen — the same
``Q(owner=user) | Q(shared_with=user)`` copy-pasted across four apps, plus four separate
``Visibility`` enums that agreed on their values and disagreed with the code that was
supposed to honour them. And that is how the field went dead without anyone noticing:

**`visibility` was read by nothing.** Not a queryset, not a permission class. A user who
marked a dataset *Shared* shared it with nobody; the seeds marked every demo object
*Shared* and every non-admin saw an empty application; the AI agent offered a laboratory
manager zero data sources, so they could not even ask a question. Eleven roles, and the
only one that could see anything was `admin`.

So: one module, one rule, and the models/views/permissions/agent all call into it.

The rule
--------
``PRIVATE``  the owner, plus anyone in ``shared_with``.
``LAB``      …plus everyone in the object's laboratory.
``ORG``      …plus every authenticated user.

Admins see everything. Visibility grants **reading** only — modifying an object remains
the owner's (or an admin's) right, whoever else can see it.

Note what ``LAB`` does when ``lab`` is NULL: ``lab__in=user.labs.all()`` matches nothing,
so it grants access to nobody. That is deliberate, and it is what makes the migration from
the old ``"shared"`` value safe — see ``datasets/migrations/0011_visibility_lab_scope.py``,
which sets out the reasoning the other three migrations point back to.
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q


class Visibility(models.TextChoices):
    """Shared by every model that has an owner. Do not redeclare this per-app."""

    PRIVATE = "private", "Private — only me"
    LAB = "lab", "My laboratory"
    ORG = "org", "Everyone in the organisation"


def _prefix(path: str) -> str:
    """`""` -> ``""``;  `"dataset"` -> ``"dataset__"``. Lets one rule serve a model and
    anything that reaches it through a relation (an alert on a dataset, a widget on a
    dashboard) — so those cannot drift from it either."""
    return f"{path}__" if path else ""


def visibility_q(user, path: str = "") -> Q:
    """The rule, as a ``Q`` — usable anywhere a queryset is built."""
    p = _prefix(path)
    return (
        Q(**{f"{p}owner": user})
        | Q(**{f"{p}shared_with": user})
        | Q(**{f"{p}visibility": Visibility.ORG})
        # A NULL lab matches no user's labs, so LAB + no lab grants nobody anything.
        | Q(**{f"{p}visibility": Visibility.LAB, f"{p}lab__in": user.labs.all()})
    )


def visible_to(qs, user, path: str = ""):
    """Narrow ``qs`` to what ``user`` may read."""
    if not (user and getattr(user, "is_authenticated", False)):
        return qs.none()
    if getattr(user, "is_admin", False):
        return qs
    return qs.filter(visibility_q(user, path)).distinct()


def can_view(obj, user) -> bool:
    """Object-level form of the same rule.

    Deliberately *not* a reimplementation: it re-runs the queryset rule against this one
    row. An object-level check that restates the list-level rule in different words is how
    the two come to disagree — and the one that disagrees quietly is the list, which is the
    one nobody tests.
    """
    if not (user and getattr(user, "is_authenticated", False)):
        return False
    if getattr(user, "is_admin", False):
        return True
    if getattr(obj, "owner_id", None) == user.id:
        return True
    return type(obj)._default_manager.filter(pk=obj.pk).filter(
        visibility_q(user)).exists()


def can_edit(obj, user) -> bool:
    """Seeing is not editing. A dataset shared with your laboratory is yours to *read*."""
    if not (user and getattr(user, "is_authenticated", False)):
        return False
    return bool(getattr(user, "is_admin", False)
                or getattr(obj, "owner_id", None) == user.id)
