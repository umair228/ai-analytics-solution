"""Seed the FULL oil-&-gas demo in one shot — meant to be run on production.

Creates everything a live demo needs, owned by / visible to the admin user:

  • Users   — the refinery org/site/section-labs and the full role-based roster
              (via ``seed_lab``): admin + managers + bench analysts + support.
  • Data    — the "Refinery LIMS" connection, 26 saved queries + cached datasets.
  • Dashboards — 6 interactive dashboards (operations, quality, instruments,
              inventory, computer-vision, documents).
  • Alerts  — 6 threshold alerts on the quality / instrument / inventory metrics.
  • Notifications — a realistic alert-event history; the newest few are left
              unacknowledged so the notification bell shows an unread badge.
  • Reports — 3 scheduled email reports (daily / weekly / monthly).

Idempotent — safe to re-run; the refinery connection's datasets, dashboards,
alerts, events and reports are rebuilt each time.

    # production (inside the api container):
    docker compose exec api python manage.py seed_demo

    python manage.py seed_demo --no-users            # platform only (users exist)
    python manage.py seed_demo --events 40           # bigger notification feed
    python manage.py seed_demo --reset-passwords     # also reset the demo creds
    python manage.py seed_demo --rebuild-warehouse   # regenerate the sqlite data
"""
from decouple import config
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Seed the full oil & gas demo: users, connection, datasets, "
            "dashboards, alerts, notifications and reports (production-ready).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--alerts-only", action="store_true",
            help="Seed ONLY alerts, notifications and reports (plus the minimal "
                 "datasets they attach to — a FK is required). No dashboards, no "
                 "extra datasets.")
        parser.add_argument(
            "--no-users", action="store_true",
            help="Skip the org/site/user roster (seed_lab); seed only the "
                 "platform. Use when the users already exist.")
        parser.add_argument(
            "--rebuild-warehouse", action="store_true",
            help="Regenerate the sqlite warehouse. Off by default — the "
                 "committed warehouse already ships in the image.")
        parser.add_argument(
            "--reset-passwords", action="store_true",
            help="Reset every seeded account back to the demo password.")
        parser.add_argument(
            "--password", default=None,
            help="Shared demo password (default: DSE_DEMO_PASSWORD env or "
                 "'Refinery@123').")
        parser.add_argument(
            "--events", type=int, default=24,
            help="Number of demo alert events (notifications) to generate.")
        parser.add_argument(
            "--no-events", action="store_true",
            help="Do not generate the alert-event notification history.")

    def handle(self, *args, **o):
        # 1) Users / org / site / section-labs ------------------------------
        if o["no_users"]:
            self.stdout.write(self.style.WARNING(
                "Skipping user seed (--no-users)."))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "Seeding users, organization and section-labs…"))
            seed_lab_kwargs = dict(
                # The committed warehouse already ships; rebuild only on request.
                no_warehouse=not o["rebuild_warehouse"],
                reset_passwords=o["reset_passwords"],
            )
            if o["password"]:
                seed_lab_kwargs["password"] = o["password"]
            call_command("seed_lab", **seed_lab_kwargs)

        # 2) Platform: connection, datasets, dashboards, alerts, events, reports
        from seed_refinery.platform_demo import (
            alert_report_dataset_keys, seed_platform,
        )

        platform_kwargs = {}
        if o["alerts_only"]:
            platform_kwargs["with_dashboards"] = False
            platform_kwargs["dataset_keys"] = alert_report_dataset_keys()
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\nSeeding alerts, notifications and reports "
                "(+ their backing datasets only)…"))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\nSeeding datasets, dashboards, alerts and notifications…"))
        counts = seed_platform(
            log=self.stdout.write,
            rich_events=not o["no_events"],
            n_events=max(0, o["events"]),
            **platform_kwargs,
        )

        # 3) Summary --------------------------------------------------------
        pw = o["password"] or config("DSE_DEMO_PASSWORD", default="Refinery@123")
        admin_user = config("DSE_ADMIN_USERNAME", default="admin")
        self.stdout.write(self.style.SUCCESS("\n✓ Oil & gas demo seeded."))
        self.stdout.write(
            f"  Connection : {counts['connection']}\n"
            f"  Datasets   : {counts['datasets']}\n"
            f"  Dashboards : {counts['dashboards']}\n"
            f"  Alerts     : {counts['alerts']}\n"
            f"  Notifications: {counts['events']} events "
            f"({counts['unread']} unread → bell badge)\n"
            f"  Reports    : {counts['reports']}"
        )
        if not o["no_users"]:
            # seed_lab only (re)sets passwords for NEW accounts unless
            # --reset-passwords, so don't assert the demo password for an admin
            # that already existed.
            cred = (f"password '{pw}'" if o["reset_passwords"]
                    else f"password '{pw}' if newly created, otherwise your "
                         f"existing password (pass --reset-passwords to force)")
            self.stdout.write(self.style.SUCCESS(
                f"\n  Sign in as '{admin_user}' ({cred}) to see the "
                f"notification bell, Alerts page and dashboards."))
