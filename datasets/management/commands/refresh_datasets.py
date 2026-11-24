"""Refresh datasets now, synchronously — for pipelines and operators.

    manage.py refresh_datasets --datasource "PMSv2 Reporting" [--datasource ...]
    manage.py refresh_datasets --all

Prints one line per dataset (rows, timing, or the error) and a summary. Exits
non-zero when any refresh failed, so a nightly pipeline step can gate on it.
(The scheduler's ``run_scheduled_refreshes`` refreshes only datasets that are
due; this command refreshes the selected ones unconditionally.)
"""
import time

from django.core.management.base import BaseCommand, CommandError

from datasets.models import Dataset
from datasets.services import datasources_by_name, refresh_many


class Command(BaseCommand):
    help = ("Refresh datasets synchronously: every dataset whose query reads the "
            "named data source(s), or --all.")

    def add_arguments(self, parser):
        parser.add_argument("--datasource", action="append", default=[], metavar="NAME",
                            help="Data source name (exact; repeatable).")
        parser.add_argument("--all", action="store_true", help="Refresh every dataset.")

    def handle(self, *args, **options):
        names, everything = options["datasource"], options["all"]
        if not names and not everything:
            raise CommandError("Give --datasource NAME (repeatable) or --all.")
        if names and everything:
            raise CommandError("Use either --datasource or --all, not both.")

        datasets = Dataset.objects.select_related("query", "query__datasource")
        if names:
            try:
                sources = datasources_by_name(names)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            datasets = datasets.filter(query__datasource__in=sources)
        datasets = list(datasets.order_by("name", "id"))
        if not datasets:
            self.stdout.write("No datasets to refresh.")
            return

        t0 = time.monotonic()
        outcomes = refresh_many(datasets)
        for outcome in outcomes:
            write = self.stdout.write if outcome.ok else self.stderr.write
            write(outcome.line())
        failed = sum(1 for o in outcomes if not o.ok)
        self.stdout.write(
            f"Refreshed {len(outcomes)} dataset(s) in {time.monotonic() - t0:.1f}s: "
            f"{len(outcomes) - failed} ok, {failed} error(s).")
        if failed:
            raise CommandError(f"{failed} of {len(outcomes)} dataset refresh(es) failed.")
