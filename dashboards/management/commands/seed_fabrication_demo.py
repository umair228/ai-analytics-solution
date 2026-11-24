"""Build the generated fabrication company and install its demonstration boards.

    manage.py seed_fabrication_demo [--out PATH] [--owner USERNAME] [--no-pack]

1. ``seed_fabrication.build_warehouse`` writes the SQLite file (deterministic).
2. A data source "Fabrication demo (generated data)" points at it, flagged
   ``options["demo"] = True``: every board reading it shows "Demonstration data".
3. ``packs/fabrication-demo/pack.yaml`` is installed (its boards are listed under
   "Fabrication demo" in a manager's sidebar).

The data is invented. Install it on a demonstration or sales environment, or next to a
client's boards only when everyone understands it is not theirs: the labels say so.
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

DEMO_SOURCE = "Fabrication demo (generated data)"
PACK = Path(settings.BASE_DIR) / "packs" / "fabrication-demo" / "pack.yaml"


class Command(BaseCommand):
    help = "Build the generated fabrication data, register it as a demo source, install its boards."

    def add_arguments(self, parser):
        parser.add_argument("--out", help="SQLite file to write (default: <BASE_DIR>/data/fabrication_demo.sqlite3)")
        parser.add_argument("--owner", help="Owner of the source and the boards (default: the first superuser)")
        parser.add_argument("--no-pack", action="store_true", help="Build and register only")

    def handle(self, *args, **opts):
        from connections.models import DataSource
        from seed_fabrication.build_warehouse import build

        users = get_user_model().objects
        owner = (users.filter(username=opts["owner"]).first() if opts.get("owner")
                 else users.filter(is_superuser=True).order_by("id").first())
        if owner is None:
            raise CommandError("No owner: pass --owner with an existing username.")
        out = Path(opts.get("out") or Path(settings.BASE_DIR) / "data" / "fabrication_demo.sqlite3").resolve()
        counts = build(out, verbose=False)
        self.stdout.write(f"Built {out}: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))

        ds = DataSource.objects.filter(name=DEMO_SOURCE).first()
        fields = {
            "source_type": "sqlite", "database_name": str(out),
            "options": {"path": str(out), "demo": True},
            "description": "Generated demonstration data for a fabrication company: "
                           "invented figures, not from any client system.",
            "visibility": "org",
        }
        if ds is None:
            ds = DataSource.objects.create(name=DEMO_SOURCE, owner=owner, **fields)
            self.stdout.write(f"Created data source '{DEMO_SOURCE}'.")
        else:
            for k, v in fields.items():
                setattr(ds, k, v)
            ds.save()
            self.stdout.write(f"Updated data source '{DEMO_SOURCE}'.")

        if not opts.get("no_pack"):
            call_command("seed_domain_pack", "--pack", str(PACK), "--owner", owner.username,
                         "--refresh", stdout=self.stdout)
