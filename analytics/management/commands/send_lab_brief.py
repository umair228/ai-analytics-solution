"""Send the overnight Lab Brief — the first thing DIS does without being asked.

Wire it to the scheduler:

    python manage.py send_lab_brief --hour 6

It no-ops cheaply outside the configured hour, so it can sit in the same 5-minute
scheduler loop as everything else.
"""
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from analytics import brief


class Command(BaseCommand):
    help = "Email the Lab Brief to lab managers / QA managers."

    # Scheduled. --hour makes it a no-op outside the configured send hour.
    periodic = True
    periodic_kwargs = {"hour": None}   # hour comes from settings/env, not the tick

    def add_arguments(self, parser):
        parser.add_argument("--hour", type=int, default=None,
                            help="Only send at this local hour (omit = send now).")
        parser.add_argument("--user", type=str, default=None,
                            help="Send to one username (default: every manager with an email).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Print the brief instead of emailing it.")
        parser.add_argument("--send-quiet", action="store_true",
                            help="Send even when nothing needs attention.")

    def handle(self, *args, **opts):
        hour = opts.get("hour")
        if hour is not None and timezone.localtime().hour != int(hour):
            return

        if opts.get("user"):
            users = User.objects.filter(username=opts["user"])
        else:
            # The brief goes to the people who can act on it. An analyst does not need
            # a nightly digest of the whole lab; a section head does.
            users = User.objects.filter(
                role__in=["admin", "lab_manager", "qa_manager", "section_head"],
                is_active=True,
            ).exclude(email="")

        if not users.exists():
            self.stdout.write("No recipients for the lab brief — skipping.")
            return

        for user in users:
            try:
                b = brief.build(user)
            except Exception as exc:  # noqa: BLE001 — one user must not stop the rest
                self.stderr.write(self.style.ERROR(
                    f"Lab brief failed for {user.username}: {exc}"))
                continue

            # A digest that manufactures urgency every night to justify itself gets
            # filtered to a folder within a fortnight.
            if b["quiet"] and not opts.get("send_quiet"):
                self.stdout.write(f"  {user.username}: all clear — not sending.")
                continue

            if opts.get("dry_run"):
                self.stdout.write(self.style.SUCCESS(f"\n=== {user.username} ==="))
                self.stdout.write(f"Subject: {b['subject']}\n")
                self.stdout.write(b["body"])
                continue

            if not user.email:
                continue
            try:
                send_mail(
                    subject=b["subject"],
                    message=b["body"] + _footer(b["facts"]),
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    recipient_list=[user.email],
                    fail_silently=False,
                )
                self.stdout.write(f"  {user.username}: sent to {user.email}")
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.WARNING(
                    f"Lab brief email failed for {user.username}: {exc}"))


def _footer(facts) -> str:
    """Provenance, on the brief as on everything else. A number in an inbox with no
    idea how old it is, is a number nobody can act on."""
    ds = (facts.get("datasource") or {}).get("name", "—")
    as_of = facts.get("data_as_of") or "live"
    return (f"\n\n—\nSource: {ds} · data as of {as_of} "
            f"(freshness: {facts.get('freshness', 'unknown')})\n"
            f"Figures come from certified SQL; the assistant only wrote the prose.\n")
