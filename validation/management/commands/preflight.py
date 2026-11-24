"""Check the runtime security posture of THIS deployment — the executable IQ.

    python manage.py preflight            # print the posture
    python manage.py preflight --strict   # exit non-zero on any FAIL (use before go-live)

Run it on the installed system. It reads the real settings and interrogates the real
databases (read-only — it never attempts a write, not even one it would roll back), and
prints evidence an assessor can paste into the Installation Qualification.
"""
from django.core.management.base import BaseCommand

from validation import preflight
from validation.preflight import FAIL, PASS, UNKNOWN, WARN


class Command(BaseCommand):
    help = "Verify the runtime security posture (Installation Qualification evidence)."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true",
                            help="Exit non-zero if any check FAILS. Use before go-live.")

    def handle(self, *args, **opts):
        import os

        from django.conf import settings

        checks = preflight.run()

        style = {PASS: self.style.SUCCESS, FAIL: self.style.ERROR,
                 WARN: self.style.WARNING, UNKNOWN: self.style.WARNING}

        module = os.environ.get("DJANGO_SETTINGS_MODULE", "?")
        self.stdout.write("\nDIS — runtime security posture\n" + "=" * 74)
        self.stdout.write(f"  settings : {module}")
        self.stdout.write(f"  app DB   : {settings.DATABASES['default']['ENGINE'].split('.')[-1]}")

        # Say plainly which findings mean anything here. A DEBUG=True failure on a
        # laptop is noise; the same output read as an audit finding would be dishonest.
        if settings.DEBUG:
            self.stdout.write(self.style.WARNING(
                "\n  ⚠ This is a DEVELOPMENT environment. The posture checks (DEBUG, "
                "SECRET_KEY, TLS,\n    image pinning) are only meaningful when run ON "
                "THE INSTALLED SYSTEM.\n    The DATA-SOURCE findings below are real "
                "wherever they run: they interrogate the\n    live LIMS, not this "
                "machine."))
        self.stdout.write("=" * 74)
        for c in checks:
            self.stdout.write(f"\n  {style[c.status](c.status):<20}{c.id:<10}{c.name}")
            self.stdout.write(f"      {c.detail}")
            if c.fix:
                for line in c.fix.splitlines():
                    self.stdout.write(self.style.WARNING(f"      → {line}"))

        fails = [c for c in checks if c.status == FAIL]
        warns = [c for c in checks if c.status == WARN]
        unknown = [c for c in checks if c.status == UNKNOWN]

        self.stdout.write("\n" + "=" * 74)
        self.stdout.write(
            f"  {len(checks) - len(fails) - len(warns) - len(unknown)} passed · "
            f"{len(fails)} FAILED · {len(warns)} warning · {len(unknown)} unknown\n")

        if unknown:
            self.stdout.write(self.style.WARNING(
                "  UNKNOWN is not a pass. A check that could not run has proven nothing."))
        if fails:
            self.stderr.write(self.style.ERROR(
                f"\n  {len(fails)} BLOCKING issue(s). This deployment is not fit for a "
                f"regulated go-live."))
            if opts["strict"]:
                raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS(
                "\n  No blocking issues. Attach this output to the IQ (URS-060 … URS-065)."))
