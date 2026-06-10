"""
Per-user retrieval/listing visibility for AI Document Search (slice 1 + follow-up).

Authorization keys on a per-chunk OWNER value carried in the index, NEVER on
doc_id — doc_ids are not unique across users (two users can approve different
content under the same doc_id, which a doc_id allowlist would leak). This is a
TEMPORARY owner/global policy; it does NOT implement org/site/lab tenancy,
shared-KB access, or per-document ACLs.

Owner encoding (see retrieval.OWNER_GLOBAL / OWNER_PRIVATE):
  * OWNER_GLOBAL   -> legacy physical-corpus chunk: visible to all authenticated users
  * "<user id>"    -> KB chunk owned by that user (created_by)
  * OWNER_PRIVATE  -> KB chunk with no/unknown owner: admin-only (fail closed)
"""
from __future__ import annotations

from .retrieval import OWNER_GLOBAL


def visible_owner_keys(user):
    """Return the set of per-chunk ``owner`` keys ``user`` may see, or ``None``
    for no restriction (admin / superuser).

    Non-admins get the global corpus plus their own KB chunks; ``OWNER_PRIVATE``
    chunks are never included, and a user with no pk gets only the global corpus
    -> fail closed.
    """
    if getattr(user, "is_admin", False):
        return None
    keys = {OWNER_GLOBAL}
    pk = getattr(user, "pk", None)
    if pk is not None:
        keys.add(str(pk))
    return keys
