"""Refresh datasets whose scheduled refresh time has arrived.

Designed to be run periodically by cron (or any scheduler), e.g. every
5 minutes:

    */5 * * * * cd /path/to/backend && .venv/bin/python manage.py run_scheduled_refreshes
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from datasets.models import Dataset
from datasets.services import refresh_dataset


class Command(BaseCommand):
    help = "Refresh datasets whose scheduled next_refresh_at has passed."

    def handle(self, *args, **options):
        now = timezone.now()
        due = Dataset.objects.exclude(
            refresh_interval=Dataset.RefreshInterval.MANUAL
        ).filter(Q(next_refresh_at__isnull=True) | Q(next_refresh_at__lte=now))

        if not due.exists():
            self.stdout.write("No datasets are due for refresh.")
            return

        ok = failed = 0
        for dataset in due:
            try:
                refresh_dataset(dataset)
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"Refreshed '{dataset.name}'"))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                # Move the schedule forward so a broken dataset doesn't spin.
                dataset.last_error = str(exc)
                dataset.schedule_next_refresh(from_time=now)
                dataset.save(update_fields=["last_error", "next_refresh_at"])
                self.stderr.write(f"Failed '{dataset.name}': {exc}")

        self.stdout.write(f"Done — {ok} refreshed, {failed} failed.")
