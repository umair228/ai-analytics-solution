"""Scheduler entry point — refresh the replica on cadence, rebuild marts if anything moved.

Invoked every few minutes by the compose `scheduler` service (alongside
run_scheduled_refreshes). It decides per table what's due:
  * never loaded            → full
  * configured nightly hour → full reconcile (catches deletes/back-dated edits)
  * hourly/daily cadence    → incremental (CHANGED_ON) when the window elapsed
  * weekly reference tables → full when >7 days old

No-ops cheaply when nothing is due. Reads DSE_REPLICATION_SOURCE_ID / _TARGET_ID /
_SCHEMA / _FULL_HOUR; does nothing (with a notice) if source/target aren't set.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from connections.engine import get_engine
from connections.models import DataSource
from replication.loader import ensure_states, replicate_table
from replication.marts import build_marts


def _due(state, now, full_hour):
    last_full, last_inc = state.last_full_at, state.last_incremental_at
    if last_full is None:
        return "full"
    if now.hour == int(full_hour) and (now - last_full) > timedelta(hours=20):
        return "full"
    if state.cadence == "weekly":
        return "full" if (now - last_full) > timedelta(days=7) else None
    window = timedelta(days=1) if state.cadence == "daily" else timedelta(hours=1)
    base = last_inc or last_full
    if base is None or (now - base) > (window - timedelta(minutes=5)):
        return "incremental"
    return None


class Command(BaseCommand):
    help = "Run due LIMS→replica refreshes and rebuild marts (scheduler-driven)."

    def handle(self, *args, **opts):
        source_id = getattr(settings, "DSE_REPLICATION_SOURCE_ID", None)
        target_id = getattr(settings, "DSE_REPLICATION_TARGET_ID", None)
        if not (source_id and target_id):
            self.stdout.write("Replication not configured (DSE_REPLICATION_SOURCE_ID/"
                              "TARGET_ID unset) — skipping.")
            return
        source = DataSource.objects.filter(pk=source_id).first()
        target = DataSource.objects.filter(pk=target_id).first()
        if not (source and target):
            self.stdout.write("Replication source/target DataSource missing — skipping.")
            return

        schema = getattr(settings, "DSE_REPLICATION_SCHEMA", "") or None
        full_hour = getattr(settings, "DSE_REPLICATION_FULL_HOUR", 2)
        now = timezone.now()

        ran = 0
        for state in ensure_states(source, target):
            mode = _due(state, now, full_hour)
            if not mode:
                continue
            r = replicate_table(state, mode=mode, target_schema=schema)
            ran += 1
            tag = "✗ " + r["error"] if r.get("error") else \
                f"[{r['mode']}] total={r['row_count']}"
            self.stdout.write(f"  {state.table_name}: {tag}")

        if ran:
            built = build_marts(get_engine(target), target.source_type, schema)
            self.stdout.write(f"Refreshed {ran} table(s); rebuilt {len(built)} marts.")
