"""Install or update a domain pack (see packs/README.md for the YAML schema).

    manage.py seed_domain_pack --pack packs/example/pack.yaml [--owner USERNAME]
                               [--recipients "a@x.com,b@y.com"] [--refresh] [--dry-run]

Validates the whole pack first (every problem is reported, nothing is written
when any is found), then creates or updates — by name — one raw-SQL query and
one dataset per pack dataset, each dashboard with its widgets (replaced
when they differ from the pack) and each scheduled report, all in one
transaction. Re-running an unchanged pack changes nothing. ``--recipients``
sets the recipients of every pack report (and turns them on; an empty value
turns them off); without it a new report is created inactive and existing
reports keep the recipients set on the Reports page. ``--dry-run`` shows what
would change and rolls back. ``--refresh`` then refreshes the pack's datasets,
printing row counts and errors (exit status 1 if any refresh failed).
"""
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from dashboards.domain_pack import PackError, apply_pack, load_pack
from datasets.services import refresh_many


class Command(BaseCommand):
    help = "Install or update a domain pack: queries, datasets, dashboards and widgets."

    def add_arguments(self, parser):
        parser.add_argument("--pack", required=True, help="Path to the pack's YAML file.")
        parser.add_argument("--owner", default="",
                            help="Username owning the pack's objects "
                                 "(default: the first active superuser).")
        parser.add_argument("--recipients", default=None,
                            help="Comma-separated email addresses for every report in the "
                                 "pack (an empty value turns the reports off). Omit to keep "
                                 "the recipients already set; new reports then start inactive.")
        parser.add_argument("--refresh", action="store_true",
                            help="Refresh the pack's datasets after applying it.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Validate and report what would change; save nothing.")

    def handle(self, *args, **options):
        path = self._resolve(options["pack"])
        try:
            pack = load_pack(path)
        except PackError as exc:
            raise CommandError(f"Pack {path} is invalid ({len(exc.errors)} problem"
                               f"{'s' if len(exc.errors) != 1 else ''}):\n  - "
                               + "\n  - ".join(exc.errors)) from exc
        owner = self._owner(options["owner"])
        recipients = self._recipients(options["recipients"])
        dry_run = options["dry_run"]

        with transaction.atomic():
            result = apply_pack(pack, owner, recipients=recipients)
            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(f"Pack '{pack.name}' v{pack.version} from {path} "
                          f"(owner: {owner.username}){' [DRY RUN]' if dry_run else ''}")
        for kind, name, action, note in result.actions:
            self.stdout.write(f"  {kind:<9} {action:<9} {name}"
                              + (f" ({note})" if note else ""))
        kinds = [("dataset", "Datasets"), ("dashboard", "Dashboards")]
        if pack.reports:
            kinds.append(("report", "Reports"))
        summary = []
        for kind, label in kinds:
            summary.append(f"{label}: " + ", ".join(
                f"{result.count(kind, a)} {a}" for a in ("created", "updated", "unchanged")))
        self.stdout.write(". ".join(summary) + ".")
        if dry_run:
            self.stdout.write("Dry run: nothing was saved"
                              + (" and nothing was refreshed." if options["refresh"] else "."))
            return

        if not options["refresh"] or not pack.datasets:
            return
        datasets = [result.datasets[d.key] for d in pack.datasets]
        t0 = time.monotonic()
        outcomes = refresh_many(datasets)
        for outcome in outcomes:
            (self.stdout.write if outcome.ok else self.stderr.write)(outcome.line())
        failed = sum(1 for o in outcomes if not o.ok)
        self.stdout.write(
            f"Refreshed {len(outcomes)} dataset(s) in {time.monotonic() - t0:.1f}s: "
            f"{len(outcomes) - failed} ok, {failed} error(s).")
        if failed:
            raise CommandError(f"Pack applied, but {failed} of {len(outcomes)} dataset "
                               "refresh(es) failed.")

    @staticmethod
    def _resolve(raw) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute() and not path.exists():
            candidate = Path(settings.BASE_DIR) / path
            if candidate.exists():
                path = candidate
        if not path.is_file():
            raise CommandError(f"Pack file not found: {raw}")
        return path.resolve()

    @staticmethod
    def _recipients(raw):
        """None = leave recipients alone; otherwise a validated list (possibly empty)."""
        if raw is None:
            return None
        emails = [e.strip() for e in raw.split(",") if e.strip()]
        bad = []
        for email in emails:
            try:
                validate_email(email)
            except ValidationError:
                bad.append(email)
        if bad:
            raise CommandError(f"--recipients: {len(bad)} invalid address"
                               f"{'es' if len(bad) != 1 else ''}: {', '.join(bad)}")
        return list(dict.fromkeys(emails))  # de-duplicated, order kept

    @staticmethod
    def _owner(username):
        User = get_user_model()
        if username:
            user = User.objects.filter(username=username).first()
            if user is None:
                raise CommandError(f"No user named {username!r} (--owner).")
            return user
        user = (User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
                or User.objects.filter(role="admin", is_active=True).order_by("id").first())
        if user is None:
            raise CommandError("No --owner given and no active admin user exists "
                               "to own the pack's objects.")
        return user
