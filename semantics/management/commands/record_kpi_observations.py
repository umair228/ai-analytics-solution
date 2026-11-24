"""Record every health tile and observed certified metric, once per snapshot (P5).

    manage.py record_kpi_observations                       # every source with a layer
    manage.py record_kpi_observations --datasource "PMSv2 Reporting"

A restored copy is identified by ``options["restore_id"]`` (written by
``promote_source``); a live source is observed once per local calendar day. Recording
is idempotent per (source, kind, key, snapshot), so the scheduler tick can run this
every few minutes: once a snapshot is recorded, a run costs one query per source.
Values are recorded whatever their classification; reading them back goes through
the same clearance as the live tiles.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from connections.models import DataSource
from semantics.health import _run_tile
from semantics.layer import get_semantic_layer
from semantics.models import KpiObservation
from semantics.spec import CLASSES

ALL_CLEARED = frozenset(CLASSES)   # the recorder is the system, not a viewer


def snapshot_id(datasource) -> str:
    restore_id = str((datasource.options or {}).get("restore_id") or "")
    if restore_id:
        return restore_id
    from zoneinfo import ZoneInfo

    local = timezone.now().astimezone(ZoneInfo(getattr(settings, "DIS_REPORT_TZ", "UTC")))
    return f"day:{local:%Y-%m-%d}"


class Command(BaseCommand):
    help = "Record health tiles and observed metrics once per source snapshot."
    periodic = True   # discovered by run_scheduler_tick

    def add_arguments(self, parser):
        parser.add_argument("--datasource", action="append", default=[],
                            help="DataSource name (repeatable); default: every source "
                                 "with a semantic layer.")

    def handle(self, *args, **opts):
        if opts["datasource"]:
            sources = []
            for name in opts["datasource"]:
                ds = DataSource.objects.filter(name=name).first()
                if ds is None:
                    raise CommandError(f"Unknown data source {name!r}.")
                sources.append(ds)
        else:
            sources = list(DataSource.objects.filter(semantic_layer__isnull=False))
        recorded = 0
        for ds in sources:
            recorded += self._record(ds)
        self.stdout.write(f"Recorded {recorded} observation(s) for {len(sources)} source(s).")

    def _record(self, ds) -> int:
        layer = get_semantic_layer(ds)
        wanted = ([(KpiObservation.Kind.TILE, t) for t in layer.health]
                  + [(KpiObservation.Kind.METRIC, m) for m in layer.metrics if m.observe])
        if not wanted:
            return 0
        snap = snapshot_id(ds)
        done = set(KpiObservation.objects.filter(datasource=ds, restore_id=snap)
                   .values_list("kind", "key"))
        todo = [(kind, item) for kind, item in wanted if (kind, item.key) not in done]
        if not todo:
            return 0
        from replication.freshness import describe

        as_of = describe(ds).data_as_of or ""
        for kind, item in todo:
            # A metric marked `observe` is a scalar by contract (the loader refuses a
            # limit on it), so it runs exactly like a tile.
            result = _run_tile(ds, item if kind == KpiObservation.Kind.TILE else _AsTile(item),
                               layer, ALL_CLEARED)
            KpiObservation.objects.get_or_create(
                datasource=ds, kind=kind, key=item.key, restore_id=snap,
                defaults={"value": result["value"], "data_as_of": as_of[:40],
                          "status": (KpiObservation.Status.ERROR if result["error"]
                                     else KpiObservation.Status.OK),
                          "error": result["error"][:2000]})
            self.stdout.write(f"  {ds.name} · {kind} {item.key} = {result['value']}"
                              + (f"  ERROR {result['error'][:120]}" if result["error"] else ""))
        return len(todo)


class _AsTile:
    """Adapts a scalar certified metric to the tile runner."""

    def __init__(self, metric):
        self.key, self.label, self.description = metric.key, metric.name, metric.description
        self.sql = metric.sql
        self.unit = metric.unit or "count"
        self.direction = metric.direction
        self.target = (metric.thresholds or {}).get("target")
        self.warn = (metric.thresholds or {}).get("warn")
        self.critical = (metric.thresholds or {}).get("critical")
        self.classes = metric.classes
