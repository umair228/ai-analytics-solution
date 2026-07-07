"""Build (and optionally prewarm) the oil-&-gas LIMS forecasting warehouse.

The warehouse is committed at forecasting/warehouse/refinery_lims.sqlite3 and
ships in the Docker image, so this is only needed to (re)generate it — e.g. if
you tweak seed_refinery/reference.json. It's deterministic (fixed RNG seed), so
a rebuild produces byte-identical data and the committed models stay valid.

    python manage.py seed_forecast_warehouse            # build the warehouse
    python manage.py seed_forecast_warehouse --prewarm  # + train all models
    python manage.py seed_forecast_warehouse --force    # rebuild even if present
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Build the LIMS forecasting SQLite warehouse (and optionally train models)."

    def add_arguments(self, parser):
        parser.add_argument("--prewarm", action="store_true",
                            help="Also train + cache every NeuralProphet model (slow).")
        parser.add_argument("--force", action="store_true",
                            help="Rebuild even if the warehouse file already exists.")

    def handle(self, *args, **opts):
        from seed_refinery.build_warehouse import WAREHOUSE, build

        target = Path(WAREHOUSE)
        if target.exists() and not opts["force"]:
            self.stdout.write(self.style.WARNING(
                f"Warehouse already exists at {target} — use --force to rebuild."))
        else:
            self.stdout.write(f"Building forecasting warehouse → {target} …")
            build(verbose=False)
            self.stdout.write(self.style.SUCCESS(f"✓ Warehouse built: {target}"))

        # Confirm the engine the forecasting views will actually read.
        eng = settings.FORECAST_DB.get("ENGINE")
        self.stdout.write(f"FORECAST_DB engine = {eng}"
                          + (f", path = {settings.FORECAST_DB.get('PATH')}"
                             if eng == "sqlite" else ""))

        if opts["prewarm"]:
            # The forecast endpoints prewarm hits require an authenticated user;
            # prewarm picks one at import and would crash on an empty user table.
            from django.contrib.auth import get_user_model
            if not get_user_model().objects.exists():
                self.stderr.write(self.style.ERROR(
                    "--prewarm needs at least one user (the forecast endpoints "
                    "require auth). Run 'manage.py bootstrap' or 'manage.py "
                    "seed_lab' first, then re-run with --prewarm."))
                return

            self.stdout.write("Prewarming forecast models (this takes several minutes) …")
            import sys

            from seed_refinery import prewarm
            # prewarm.main() reads sys.argv[1:] to pick features; under manage.py
            # that would be the command args, so force the "all features" default.
            saved = sys.argv
            sys.argv = ["prewarm"]
            try:
                prewarm.main()
            finally:
                sys.argv = saved
            self.stdout.write(self.style.SUCCESS("✓ Models prewarmed."))
