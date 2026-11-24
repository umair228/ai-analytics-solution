"""Compare each certified KPI with its reference figure (the R2 exit check).

    manage.py check_metric_parity --datasource "PMSv2 Reporting"
    manage.py check_metric_parity --datasource "PMSv2 Reporting" --file semantics/specs/fmms.yaml

The spec names the reference in a ``parity:`` block, e.g. the reporting layer's own
summary computed independently of DIS::

    parity:
      sql: "SELECT value FROM {{table:exec_summary}} WHERE kpi_id = :kpi_id"
      tolerance: {count: 0, percent: 0.5, days: 0.05}

Every loaded metric with a ``kpi_id`` and ``observe: true`` (a scalar) is run and
compared. Tolerances are absolute, per unit (default 0). Exits non-zero on any
difference or error, so a release pipeline can gate on it.
"""
from django.core.management.base import BaseCommand, CommandError

# The module, not the function: a name bound at import time would keep whatever object
# was there then (the scheduler tick imports every command, sometimes under a test's
# patch), so the reference query could run through a stale stand-in.
from querybuilder import executor
from semantics.health import _run_tile
from semantics.layer import get_semantic_layer, render_for
from semantics.management.commands.load_semantic_layer import Command as LoadCommand
from semantics.management.commands.load_semantic_layer import resolve_datasource
from semantics.management.commands.record_kpi_observations import ALL_CLEARED, _AsTile
from semantics.spec import load_yaml


class Command(BaseCommand):
    help = "Compare certified KPIs with the reference figures named in the spec."

    def add_arguments(self, parser):
        parser.add_argument("--datasource", required=True, help="DataSource id or name.")
        parser.add_argument("--file", default=None, help="The spec (default: as the loader finds it).")

    def handle(self, *args, **opts):
        ds = resolve_datasource(opts["datasource"])
        spec = load_yaml(opts["file"] or LoadCommand()._find_spec(ds))
        parity = spec.get("parity") or {}
        if not parity.get("sql"):
            raise CommandError("The spec has no `parity: {sql: ...}` block.")
        tolerance = {k: float(v) for k, v in (parity.get("tolerance") or {}).items()}
        layer = get_semantic_layer(ds)
        if not layer.is_present():
            raise CommandError(f"No semantic layer is loaded for '{ds.name}'.")
        reference_sql = render_for(layer, parity["sql"], ds)

        checked = failed = 0
        for metric in sorted(layer.metrics, key=lambda m: m.key):
            if not (metric.kpi_id and metric.observe):
                continue
            checked += 1
            mine = _run_tile(ds, _AsTile(metric), layer, ALL_CLEARED)
            try:
                rows = executor.execute_raw_sql(
                    ds, reference_sql, params={"kpi_id": metric.kpi_id}, max_rows=1,
                ).get("rows") or []
                ref = rows[0][0] if rows and rows[0] else None
                ref = None if ref in (None, "") else float(ref)
            except Exception as exc:  # noqa: BLE001
                ref, ref_error = None, str(exc).splitlines()[0][:160]
            else:
                ref_error = ""
            allowed = tolerance.get(metric.unit or "count", 0.0)
            value = mine["value"]
            if mine["error"] or ref_error:
                verdict, note = "ERROR", mine["error"][:160] or ref_error
            elif value is None or ref is None:
                verdict = "PASS" if value is None and ref is None else "FAIL"
                note = "no value" if verdict == "PASS" else "one side has no value"
            else:
                diff = abs(float(value) - ref)
                verdict = "PASS" if diff <= allowed + 1e-9 else "FAIL"
                note = f"diff {diff:g} (tolerance {allowed:g} {metric.unit or 'count'})"
            failed += verdict != "PASS"
            self.stdout.write(f"{verdict:5s} {metric.kpi_id:8s} {metric.key:32s} "
                              f"metric={value!s:>12s} reference={ref!s:>12s}  {note}")
        self.stdout.write(f"{checked} KPI(s) checked, {failed} not matching.")
        if failed:
            raise CommandError(f"{failed} certified KPI(s) differ from the reference.")
