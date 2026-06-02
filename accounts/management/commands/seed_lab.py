"""Seed the refinery laboratory: organization, site, the 8 section "labs", and a
full role-based user roster (management, bench analysts and support staff), then
rebuild the operational warehouse so the people who appear in the LIMS data also
have login accounts.

The bench analysts are read from ``seed_refinery/reference.json`` (so they match
the warehouse exactly); the management/support staff come from the shared
``seed_refinery.personnel`` roster.

Idempotent — safe to run repeatedly. By default a re-run only updates profile
fields and creates any missing accounts; existing passwords are left untouched
(so a manually-changed admin password is never silently clobbered). Newly
created accounts always get the shared demo password. Pass ``--reset-passwords``
to force every seeded account back to the demo password (e.g. to refresh the
credential sheet).

    python manage.py seed_lab                    # full seed + warehouse rebuild
    python manage.py seed_lab --no-warehouse     # users/org only
    python manage.py seed_lab --reset-passwords  # also reset existing passwords
    python manage.py seed_lab --password XXXX    # custom shared demo password
"""
import json

from decouple import config
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Lab, Organization, Site
from seed_refinery import build_warehouse
from seed_refinery.personnel import (
    SECTION_NAME,
    SUPPORT_STAFF,
    WAREHOUSE_ROLE_TO_DJANGO,
    email_for,
    split_name,
    username_for,
)

User = get_user_model()

ORG_NAME = "Refinery Industries"
ORG_CODE = "REF"
SITE_NAME = "Main Refinery Complex"
SITE_CODE = "MAIN"

# Roles that should also be able to reach the Django admin site.
STAFF_ROLES = {User.Role.ADMIN, User.Role.LAB_MANAGER, User.Role.QA_MANAGER}


class Command(BaseCommand):
    help = (
        "Create the refinery org/site/section-labs and the full role-based user "
        "roster, then rebuild the operational warehouse."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-warehouse",
            action="store_true",
            help="Skip rebuilding media/refinery_lims.sqlite3.",
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Reset every seeded account (incl. existing ones) to the demo "
                 "password. Off by default so existing passwords are preserved.",
        )
        parser.add_argument(
            "--password",
            default=config("DSE_DEMO_PASSWORD", default="Refinery@123"),
            help="Shared demo password applied to new (and, with "
                 "--reset-passwords, existing) accounts.",
        )

    def handle(self, *args, **options):
        self.password = options["password"]
        self.reset_passwords = options["reset_passwords"]

        # All ORM mutations happen atomically; the (long, file-destroying)
        # warehouse rebuild runs separately AFTER the accounts have committed.
        org, site, labs, created, updated, roster, admin_username = (
            self._seed_accounts()
        )

        # ---- Summary ---------------------------------------------------------
        self.stdout.write(self.style.SUCCESS(
            f"\nOrganization '{org.name}' / Site '{site.name}' / "
            f"{len(labs)} section labs ready."
        ))
        pw_note = (
            f"shared password reset to '{self.password}'"
            if self.reset_passwords
            else f"new accounts use password '{self.password}'; existing "
                 f"passwords preserved"
        )
        self.stdout.write(self.style.SUCCESS(
            f"Users: {created} created, {updated} updated ({pw_note}).\n"
        ))
        self.stdout.write(f"  {'USERNAME':24} {'ROLE':34} SECTION")
        self.stdout.write(f"  {'-'*24} {'-'*34} {'-'*22}")
        self.stdout.write(
            f"  {admin_username:24} {'Administrator':34} Laboratory-wide"
        )
        for username, label, section in roster:
            self.stdout.write(f"  {username:24} {label:34} {section}")

        # ---- Operational warehouse (outside the DB transaction) -------------
        if options["no_warehouse"]:
            self.stdout.write(self.style.WARNING(
                "\nSkipped warehouse rebuild (--no-warehouse)."
            ))
        else:
            self.stdout.write("\nRebuilding operational warehouse …")
            counts = build_warehouse.build(verbose=False)
            self.stdout.write(self.style.SUCCESS(
                f"Warehouse rebuilt: {counts.get('SAMPLE', 0):,} samples, "
                f"{counts.get('test_results', 0):,} test results, "
                f"{counts.get('analysts', 0)} analysts."
            ))

    # ------------------------------------------------------------------------
    @transaction.atomic
    def _seed_accounts(self):
        """Create/refresh the org, site, section-labs and the full user roster.

        Returns (org, site, labs, created, updated, roster, admin_username).
        """
        # ---- Organization / Site / Section-Labs -----------------------------
        org, _ = Organization.objects.get_or_create(
            name=ORG_NAME, defaults={"code": ORG_CODE}
        )
        site, _ = Site.objects.get_or_create(
            organization=org, name=SITE_NAME,
            defaults={"code": SITE_CODE, "location": "Refinery Complex"},
        )
        labs = {}  # section_code -> Lab
        for code, name in SECTION_NAME.items():
            lab, _ = Lab.objects.get_or_create(
                site=site, name=name, defaults={"code": code}
            )
            labs[code] = lab
        self.org, self.site, self.labs = org, site, labs
        all_labs = list(labs.values())

        created = updated = 0

        # ---- Administrator ---------------------------------------------------
        admin_username = config("DSE_ADMIN_USERNAME", default="admin")
        admin_email = config("DSE_ADMIN_EMAIL", default="admin@dse.local")
        c = self._upsert(
            username=admin_username,
            first_name="Platform",
            last_name="Administrator",
            email=admin_email,
            role=User.Role.ADMIN,
            labs=all_labs,
            job_title="System Administrator",
            is_staff=True,
            is_superuser=True,
        )
        created, updated = self._tally(c, created, updated)

        roster = []  # (username, role_label, section_label) for the summary

        # ---- Bench analysts (from reference.json) ----------------------------
        ref = json.loads(build_warehouse.REF_PATH.read_text())
        for a in ref["analysts"]:
            role = WAREHOUSE_ROLE_TO_DJANGO.get(a["role"], User.Role.ANALYST)
            section_code = build_warehouse.canon(a["section_code"])
            first, last = split_name(a["name"])
            username = username_for(a["name"])
            section_name = SECTION_NAME.get(section_code, "Laboratory")
            label = User.Role(role).label
            c = self._upsert(
                username=username,
                first_name=first,
                last_name=last,
                email=email_for(username),
                role=role,
                labs=[labs[section_code]] if section_code in labs else all_labs,
                job_title=f"{label} – {section_name}",
                is_staff=role in STAFF_ROLES,
                is_superuser=False,
            )
            created, updated = self._tally(c, created, updated)
            roster.append((username, label, section_name))

        # ---- Management & support staff (from personnel roster) --------------
        for p in SUPPORT_STAFF:
            role = p["role"]
            section_code = p["section_code"]
            first, last = split_name(p["name"])
            username = username_for(p["name"])
            section_name = (
                SECTION_NAME.get(section_code) if section_code else None
            )
            label = User.Role(role).label
            # Lab-wide staff (no section) are scoped to every lab.
            user_labs = [labs[section_code]] if section_code in labs else all_labs
            job_title = f"{label} – {section_name}" if section_name else label
            c = self._upsert(
                username=username,
                first_name=first,
                last_name=last,
                email=email_for(username),
                role=role,
                labs=user_labs,
                job_title=job_title,
                is_staff=p.get("is_staff", False),
                is_superuser=False,
            )
            created, updated = self._tally(c, created, updated)
            roster.append((username, label, section_name or "Laboratory-wide"))

        return org, site, labs, created, updated, roster, admin_username

    # ------------------------------------------------------------------------
    def _upsert(self, *, username, first_name, last_name, email, role,
                labs, job_title, is_staff, is_superuser):
        """Create or update a single user. Returns True if newly created.

        The password is only (re)set for newly created accounts, or for every
        account when --reset-passwords is given, so existing credentials are
        never silently overwritten on a routine re-run.
        """
        user, created = User.objects.get_or_create(username=username)
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.role = role
        user.organization = self.org
        user.job_title = job_title
        user.is_active = True
        user.is_staff = is_staff
        user.is_superuser = is_superuser
        if created or self.reset_passwords:
            user.set_password(self.password)
        user.save()
        user.sites.set([self.site])
        user.labs.set(labs)
        return created

    @staticmethod
    def _tally(created, c_count, u_count):
        return (c_count + 1, u_count) if created else (c_count, u_count + 1)
