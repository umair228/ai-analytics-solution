"""Point data source(s) at a freshly restored database (A/B promote).

    manage.py promote_source --datasource "PMSv2 Reporting" \\
        --datasource "PMSv2 Reporting - Restricted" \\
        --database fmms_rpt_b --restore-id 2026-11-23T02:10 --refresh

For each named DataSource: ``database_name`` becomes ``--database`` (for a
SQLite source that reads its file from ``options.path``, that path too), and
``options`` records ``restore_id``, ``promoted_at`` and ``previous_database``
(the value to promote back to for a rollback). The change and one audit row per
source (action UPDATE) commit in a single transaction — if the audit row cannot
be written, nothing is promoted. ``--refresh`` then refreshes every dataset whose
query reads those sources and exits non-zero if any refresh failed (the
promotion itself stays applied).

Saved queries with their own ``database`` override keep reading that database;
the command lists them.
"""
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from connections.constants import SourceType
from core.audit import AuditWriteError, record_audit_strict
from core.models import AuditLog
from datasets.models import Dataset
from datasets.services import datasources_by_name, refresh_many
from querybuilder.models import QueryDefinition


class Command(BaseCommand):
    help = ("Switch data source(s) to a newly restored database, audit it, and "
            "optionally refresh the datasets that read them.")

    def add_arguments(self, parser):
        parser.add_argument("--datasource", action="append", required=True, metavar="NAME",
                            help="Data source name (exact; repeatable).")
        parser.add_argument("--database", required=True,
                            help="Database to point the data source(s) at.")
        parser.add_argument("--restore-id", default="",
                            help="Identifier of the restore being promoted (stored in options).")
        parser.add_argument("--refresh", action="store_true",
                            help="Refresh every dataset that reads the promoted source(s).")

    def handle(self, *args, **options):
        database = (options["database"] or "").strip()
        if not database:
            raise CommandError("--database must not be empty.")
        restore_id = (options["restore_id"] or "").strip()
        try:
            sources = datasources_by_name(options["datasource"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        now = timezone.now()
        done = []
        try:
            with transaction.atomic():
                for source in sources:
                    previous = source.database_name
                    opts = dict(source.options or {})
                    if source.source_type == SourceType.SQLITE and opts.get("path"):
                        opts["path"] = database  # SQLite prefers options.path
                    if restore_id:
                        opts["restore_id"] = restore_id
                    else:
                        opts.pop("restore_id", None)  # never leave a stale id behind
                    opts["promoted_at"] = now.isoformat()
                    opts["previous_database"] = previous
                    source.database_name = database
                    source.options = opts
                    source.save(update_fields=["database_name", "options", "updated_at"])
                    record_audit_strict(
                        AuditLog.Action.UPDATE, target_type="DataSource",
                        target_id=source.id,
                        summary=f"Promoted '{source.name}' to database '{database}'",
                        detail={"operation": "promote_source", "datasource": source.name,
                                "previous_database": previous, "database": database,
                                "restore_id": restore_id or None,
                                "promoted_at": now.isoformat()},
                    )
                    suffix = f" (restore {restore_id})" if restore_id else ""
                    done.append(f"Promoted '{source.name}' (#{source.id}): "
                                f"{previous or '-'} -> {database}{suffix}")
        except AuditWriteError as exc:
            raise CommandError(
                f"Nothing was promoted: the audit row could not be written ({exc}).") from exc
        for line in done:  # only after the transaction committed
            self.stdout.write(line)

        pinned = list(QueryDefinition.objects.filter(datasource__in=sources)
                      .exclude(database="").values_list("name", "database"))
        if pinned:
            self.stderr.write(
                f"Note: {len(pinned)} saved quer{'y' if len(pinned) == 1 else 'ies'} "
                "override the database and still read it: "
                + "; ".join(f"{n} -> {db}" for n, db in pinned))

        if not options["refresh"]:
            return
        datasets = list(Dataset.objects.select_related("query", "query__datasource")
                        .filter(query__datasource__in=sources).order_by("name", "id"))
        if not datasets:
            self.stdout.write("No datasets read the promoted source(s).")
            return
        t0 = time.monotonic()
        outcomes = refresh_many(datasets)
        for outcome in outcomes:
            (self.stdout.write if outcome.ok else self.stderr.write)(outcome.line())
        failed = sum(1 for o in outcomes if not o.ok)
        self.stdout.write(
            f"Refreshed {len(outcomes)} dataset(s) in {time.monotonic() - t0:.1f}s: "
            f"{len(outcomes) - failed} ok, {failed} error(s).")
        if failed:
            raise CommandError(
                f"Promotion applied, but {failed} of {len(outcomes)} dataset "
                "refresh(es) failed.")
