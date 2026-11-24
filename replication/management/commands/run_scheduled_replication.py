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
from replication.loader import ensure_states, replicate_table, stale_states
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

    # Scheduled. No-ops cheaply until the replication source/target are configured.
    periodic = True

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

        self._alert_if_stale(source, target)

    def _alert_if_stale(self, source, target):
        """Raise the stale-replica alert.

        DIS_TalkToData_Plan.md §5 calls this "the linchpin of the freshness
        guarantee". ``stale_states()`` was written for it and then called by
        nothing — so a silently-failing replication served stale data with full
        confidence, indefinitely, and no one would know. Failing loudly here is the
        difference between a replica you can trust and one you merely hope about.
        """
        stale = stale_states(source=source, target=target)
        if not stale:
            return

        lines = []
        for s in stale:
            synced = s.last_synced_at
            why = s.last_error or (
                "never synced" if synced is None
                else f"last synced {synced:%Y-%m-%d %H:%M} UTC (cadence: {s.cadence})")
            lines.append(f"  • {s.table_name}: {why}")
        body = "\n".join(lines)

        self.stderr.write(self.style.ERROR(
            f"STALE REPLICA — {len(stale)} table(s) overdue on '{target.name}':\n{body}\n"
            "Answers from this datasource are being served with freshness='stale'."))

        recipients = [a for a in (getattr(settings, "DSE_ALERT_EMAILS", None) or []) if a]
        if not recipients:
            return
        try:
            from django.core.mail import send_mail
            send_mail(
                subject=f"[DIS] Stale replica: {len(stale)} table(s) overdue on {target.name}",
                message=(f"The LIMS replica '{target.name}' is overdue.\n\n{body}\n\n"
                         "Answers are being stamped freshness='stale' until it catches up."),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=recipients,
                fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001 - the console alert above already fired
            self.stderr.write(self.style.WARNING(f"Stale-replica email failed: {exc}"))
