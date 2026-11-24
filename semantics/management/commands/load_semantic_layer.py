"""Load a curated semantic-layer YAML into the runtime models.

    python manage.py load_semantic_layer --datasource EGPC_DEV
    python manage.py load_semantic_layer --datasource 1 --file semantics/specs/egpc_dev.yaml
    python manage.py load_semantic_layer --datasource 1 --no-verify   # skip live-schema check

By default it verifies that every table/column/join in the spec exists in the live
schema and FAILS LOUDLY on drift (so a renamed LIMS column can't silently rot the
data dictionary).
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from connections.models import DataSource
from semantics.spec import SpecError, apply_spec, load_yaml


def resolve_datasource(ident):
    if ident is None:
        raise CommandError("--datasource is required (id or name).")
    ds = None
    if str(ident).isdigit():
        ds = DataSource.objects.filter(pk=int(ident)).first()
    if ds is None:
        ds = DataSource.objects.filter(name__iexact=str(ident)).first()
    if ds is None:
        raise CommandError(f"No DataSource matching '{ident}'.")
    return ds


class Command(BaseCommand):
    help = "Load a semantic-layer YAML spec into the data-dictionary models."

    def add_arguments(self, parser):
        parser.add_argument("--datasource", required=True, help="DataSource id or name.")
        parser.add_argument("--file", default=None, help="Path to the YAML spec.")
        parser.add_argument("--no-verify", action="store_true",
                            help="Skip the live-schema drift check.")

    def handle(self, *args, **opts):
        ds = resolve_datasource(opts["datasource"])
        path = opts["file"] or self._find_spec(ds)

        spec = load_yaml(path)
        try:
            counts = apply_spec(ds, spec, verify=not opts["no_verify"])
        except SpecError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"Loaded semantic layer for '{ds.name}' from {Path(path).name}: "
            f"{counts['tables']} tables, {counts['columns']} columns, "
            f"{counts['joins']} joins, {counts['metrics']} metrics."))

    # ------------------------------------------------------------------
    def _find_spec(self, ds) -> Path:
        """Work out which spec belongs to this data source.

        The naming does not line up and it cost somebody ten minutes. Specs are named after
        the **database** (``egpc_dev.yaml``, because the database is ``EGPC_DEV``), but the
        derived path used only the **data source name** (``EGPC LIMS`` -> ``egpc_lims.yaml``)
        — so the convenience default could never match, and the example command in the spec's
        own header (``--datasource EGPC_DEV``) named a data source that does not exist.

        So: try both names, then fall back to the spec that *declares* this database in its
        ``datasource:`` field — and if none of that works, say what IS available rather than
        printing one path that was never going to exist.
        """
        specs_dir = Path(settings.BASE_DIR) / "semantics" / "specs"

        def slug(text):
            return str(text or "").strip().lower().replace(" ", "_").replace("-", "_")

        for candidate in (slug(ds.name), slug(ds.database_name)):
            if candidate:
                path = specs_dir / f"{candidate}.yaml"
                if path.exists():
                    return path

        # Last resort: a spec whose own `datasource:` field names this source or database.
        wanted = {slug(ds.name), slug(ds.database_name)} - {""}
        for path in sorted(specs_dir.glob("*.yaml")):
            try:
                declared = slug((load_yaml(path) or {}).get("datasource"))
            except Exception:  # noqa: BLE001 — a broken spec must not hide the good ones
                continue
            if declared and declared in wanted:
                return path

        available = sorted(p.name for p in specs_dir.glob("*.yaml"))
        raise CommandError(
            f"No spec found for data source '{ds.name}' (database "
            f"'{ds.database_name or '?'}').\n"
            f"  Tried: {slug(ds.name)}.yaml, {slug(ds.database_name)}.yaml, and every spec "
            f"declaring one of those in its `datasource:` field.\n"
            f"  Available specs: {', '.join(available) or '(none)'}\n"
            f"  Name it explicitly:  --datasource {ds.id} --file semantics/specs/<one of the above>"
        )
