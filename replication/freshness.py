"""Is the datasource we just queried live, or a replica — and if a replica, how old?

``texttosql.runner`` used to stamp every answer ``freshness: "live"`` as a hardcoded
string literal, and ``data_as_of`` was reserved in the export provenance footer but
never set by any code path. An answer computed from a three-day-stale replica was
therefore byte-identical, in its provenance, to a genuinely live one — the system
could not tell a customer how old its own numbers were.

DIS_TalkToData_Plan.md §5 calls the stale-replica alert "the linchpin of the
freshness guarantee". ``loader.stale_states()`` was written for it and then called
by nothing: no view, no command, no alert. This module is that linchpin, and
``run_scheduled_replication`` now raises the alert.

The rule that matters: **the slowest table bounds the answer.** A query joining
SAMPLE and RESULT is only as fresh as the staler of the two, so ``data_as_of`` is
the MINIMUM last-sync across the tables of the replica — never the maximum, which
would flatter the answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Freshness:
    """How current the data behind an answer is."""

    kind: str = "live"                     # live | replica | snapshot | stale | unknown
    data_as_of: str | None = None          # ISO-8601; None for a live source
    stale_tables: list = field(default_factory=list)
    detail: str = ""

    def warnings(self) -> list[str]:
        if self.kind != "stale":
            return []
        tables = ", ".join(self.stale_tables[:5]) or "one or more tables"
        return [f"The replica is overdue for {tables} — this answer may be stale."]

    def as_provenance(self) -> dict:
        return {
            "freshness": self.kind,
            "data_as_of": self.data_as_of,
            "freshness_detail": self.detail,
        }


SNAPSHOT_MAX_AGE_HOURS = 26     # a nightly copy older than this missed a night
_SNAPSHOT_CACHE_SECONDS = 300


def _snapshot(datasource) -> Freshness | None:
    """Freshness of a source that is a periodically RESTORED COPY of a system.

    A copy declares ``options["freshness_sql"]``: one read-only query returning when
    its data was taken (PMSv2: the nightly backup time, Riyadh). Such a source is not
    "live" — its numbers describe the moment of the backup — so answers and tiles say
    "snapshot, as of …", and "stale" once it is older than
    ``options["freshness_max_age_hours"]`` (default 26: one missed night). Naive
    timestamps are local time in DIS_REPORT_TZ, as the export stamps read them.
    Returns None for a source without the option.
    """
    options = getattr(datasource, "options", None) or {}
    sql = str(options.get("freshness_sql") or "").strip()
    if not sql:
        return None
    from datetime import datetime
    from datetime import timezone as dt_timezone
    from zoneinfo import ZoneInfo

    from django.conf import settings
    from django.core.cache import cache

    restore_id = str(options.get("restore_id") or "")
    key = f"freshness-snapshot:{datasource.pk}:{restore_id}"
    value = cache.get(key)
    if value is None:
        try:
            from querybuilder.executor import assert_read_only, execute_raw_sql

            assert_read_only(sql)
            rows = execute_raw_sql(datasource, sql, max_rows=1).get("rows") or []
            value = rows[0][0] if rows and rows[0] else ""
        except Exception:  # noqa: BLE001 - never fail an answer over its own metadata
            return Freshness(kind="unknown", detail="The source's freshness query failed.")
        cache.set(key, value if value is not None else "", _SNAPSHOT_CACHE_SECONDS)
    if value in (None, ""):
        return Freshness(kind="unknown",
                         detail="The source did not report when its data was taken.")
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return Freshness(kind="snapshot", data_as_of=str(value),
                         detail="A restored copy of the source system.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(getattr(settings, "DIS_REPORT_TZ", "UTC")))
    hours = (datetime.now(dt_timezone.utc) - when).total_seconds() / 3600.0
    max_age = float(options.get("freshness_max_age_hours") or SNAPSHOT_MAX_AGE_HOURS)
    taken = f"{when:%Y-%m-%d %H:%M} {when.tzname() or ''}".strip()
    if hours > max_age:
        return Freshness(
            kind="stale", data_as_of=when.isoformat(),
            detail=(f"A restored copy taken {hours:.0f} h ago, at {taken}: more than "
                    f"{max_age:g} h, so the scheduled refresh is overdue."))
    return Freshness(
        kind="snapshot", data_as_of=when.isoformat(),
        detail=(f"A restored copy of the source system, as of {taken}"
                + (f"; restore {restore_id}" if restore_id else "") + "."))


def describe(datasource) -> Freshness:
    """Freshness of ``datasource`` — cheap enough to call on every question."""
    snapshot = _snapshot(datasource)
    if snapshot is not None:
        return snapshot
    try:
        from .models import ReplicationState
        states = list(ReplicationState.objects.filter(target=datasource))
    except Exception:  # noqa: BLE001 - never fail an answer over its own metadata
        return Freshness(kind="unknown", detail="Could not determine data freshness.")

    if not states:
        # Nothing replicates INTO this datasource, so it is the system of record.
        return Freshness(kind="live", detail="Queried the source system directly.")

    stale = sorted(s.table_name for s in states if s.is_stale())
    synced = [s for s in states if s.last_synced_at]

    # The slowest table bounds it. NB that is often a *reference* table on a weekly
    # cadence (PRODUCT_SPEC, LIMS_USERS) which is perfectly healthy — being weekly is
    # its design, not a fault. So we stay conservative (never overstate how current an
    # answer is) but NAME the bounding table, because an unexplained "2 days old" next
    # to a green badge reads as a contradiction rather than as a fact.
    oldest = min(synced, key=lambda s: s.last_synced_at) if synced else None
    as_of = oldest.last_synced_at if oldest else None
    as_of_iso = as_of.isoformat() if as_of else None

    if stale:
        return Freshness(
            kind="stale", data_as_of=as_of_iso, stale_tables=stale,
            detail=(f"Replica: {len(stale)} of {len(states)} tables are overdue "
                    f"({', '.join(stale[:5])})."),
        )
    if oldest is None:
        return Freshness(
            kind="stale", data_as_of=None, stale_tables=sorted(s.table_name for s in states),
            detail="Replica has never completed a sync.",
        )
    return Freshness(
        kind="replica", data_as_of=as_of_iso,
        detail=(f"Read from the analytics replica. Every table is within its sync "
                f"window; the oldest is {oldest.table_name} "
                f"({oldest.cadence}, last synced {as_of:%Y-%m-%d %H:%M} UTC), which "
                f"bounds how current an answer can be."),
    )
