"""Mirror the LIMS into the analytics replica and (re)build the marts.

    # full reconcile of every configured table, then rebuild marts
    python manage.py replicate_lims --source-id 1 --target-id 2 --mode full

    # incremental (CHANGED_ON) pull + upsert, then rebuild marts
    python manage.py replicate_lims --source-id 1 --target-id 2 --mode incremental

    # just rebuild marts from the current replica
    python manage.py replicate_lims --target-id 2 --mode marts

    # see the extract SQL without running anything
    python manage.py replicate_lims --source-id 1 --target-id 2 --dry-run

source/target are DataSource ids (source = live LIMS, target = the replica — a
Postgres schema in prod, SQLite in dev). Defaults come from
settings.DSE_REPLICATION_SOURCE_ID / _TARGET_ID / _SCHEMA. NEVER point the
text-to-SQL tool at the source — point it at the target. (DIS_TalkToData_Plan §5)
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from connections.engine import get_engine
from connections.models import DataSource
from replication.loader import replicate_all
from replication.marts import build_marts


class Command(BaseCommand):
    help = "Replicate LIMS tables into the analytics replica and build marts."

    def add_arguments(self, parser):
        parser.add_argument("--source-id", type=int, default=None)
        parser.add_argument("--target-id", type=int, default=None)
        parser.add_argument("--mode", choices=["full", "incremental", "marts"],
                            default="incremental")
        parser.add_argument("--schema", default=None, help="Target schema (Postgres).")
        parser.add_argument("--tables", default=None, help="Comma-separated subset.")
        parser.add_argument("--dry-run", action="store_true")

    def _ds(self, ds_id, label):
        if ds_id is None:
            raise CommandError(f"--{label}-id is required (or set DSE_REPLICATION_{label.upper()}_ID).")
        ds = DataSource.objects.filter(pk=ds_id).first()
        if ds is None:
            raise CommandError(f"No DataSource with id {ds_id} for --{label}-id.")
        return ds

    def handle(self, *args, **opts):
        source_id = opts["source_id"] or getattr(settings, "DSE_REPLICATION_SOURCE_ID", None)
        target_id = opts["target_id"] or getattr(settings, "DSE_REPLICATION_TARGET_ID", None)
        schema = opts["schema"] or (getattr(settings, "DSE_REPLICATION_SCHEMA", "") or None)
        mode = opts["mode"]

        target = self._ds(target_id, "target")

        if mode == "marts":
            built = build_marts(get_engine(target), target.source_type, schema)
            for b in built:
                self.stdout.write(f"  mart {b['mart']:<16} {b['rows']} rows")
            self.stdout.write(self.style.SUCCESS(f"Built {len(built)} marts on '{target.name}'."))
            return

        source = self._ds(source_id, "source")
        only = [t.strip() for t in opts["tables"].split(",")] if opts["tables"] else None
        self.stdout.write(f"Replicating {source.name} → {target.name} "
                          f"(mode={mode}, schema={schema or '-'})…")

        results = replicate_all(source, target, mode=mode, target_schema=schema,
                                only_tables=only, dry_run=opts["dry_run"])
        ok = errs = 0
        for r in results:
            if opts["dry_run"]:
                self.stdout.write(f"  [{r['mode']}] {r['table']}: {r['sql']}")
                continue
            if r.get("error"):
                errs += 1
                self.stdout.write(self.style.WARNING(f"  ✗ {r['table']}: {r['error']}"))
            else:
                ok += 1
                self.stdout.write(f"  ✓ {r['table']:<14} [{r['mode']}] "
                                  f"pulled={r['rows_pulled']} total={r['row_count']} "
                                  f"wm={r.get('high_watermark') or '-'}")

        if opts["dry_run"]:
            return

        built = build_marts(get_engine(target), target.source_type, schema)
        for b in built:
            self.stdout.write(f"  mart {b['mart']:<16} {b['rows']} rows")
        msg = f"Replicated {ok} table(s), built {len(built)} marts"
        self.stdout.write((self.style.WARNING if errs else self.style.SUCCESS)(
            msg + (f", {errs} FAILED — check ReplicationState.last_error." if errs else ".")))
