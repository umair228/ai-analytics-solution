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
        path = opts["file"]
        if not path:
            slug = ds.name.lower().replace(" ", "_")
            path = Path(settings.BASE_DIR) / "semantics" / "specs" / f"{slug}.yaml"
        if not Path(path).exists():
            raise CommandError(f"Spec file not found: {path}")

        spec = load_yaml(path)
        try:
            counts = apply_spec(ds, spec, verify=not opts["no_verify"])
        except SpecError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"Loaded semantic layer for '{ds.name}': "
            f"{counts['tables']} tables, {counts['columns']} columns, "
            f"{counts['joins']} joins, {counts['metrics']} metrics."))
