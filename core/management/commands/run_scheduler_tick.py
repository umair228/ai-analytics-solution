"""One tick of the background scheduler: run every command that represents recurring work.

This exists because the same bug has now happened twice.

The production scheduler used to be a shell loop in docker-compose that named each periodic
command by hand. `run_scheduled_replication` was left out of it, so the entire replication app was
inert in production — and nobody noticed, because nothing failed. Later `send_scheduled_reports`
turned out to have been left out too, so every scheduled report a user created was written to the
database, marked active, shown in the UI as scheduled, and never sent. Not once.

Neither of those was a crash. Both were a feature that reported success and quietly did nothing,
which is the worst kind of defect to ship: there is no error to notice, no log line to grep, and
the user has no reason to doubt what the screen told them.

The fix is to stop maintaining the list by hand. A command declares itself periodic by setting
`periodic = True`, this tick discovers every such command and runs it, and a test fails the build
if a periodic command exists that this tick would not run. Forgetting to wire one up is no longer
possible — the only way to be forgotten is to not ask to be scheduled.

The contract for a periodic command is simple and each of them already honours it:

    It may be invoked every few minutes, and must decide for itself whether it is due.

So `send_scheduled_reports` checks `last_sent_at`, `run_scheduled_refreshes` checks
`next_refresh_at`, and so on. The tick is dumb on purpose.
"""
import logging
import time

from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def periodic_commands():
    """Every management command that has asked to be run on the scheduler, in a stable order.

    Discovered, not listed — a list is a thing you can forget to add to.
    """
    found = []
    for name, app in sorted(get_commands().items()):
        if app.startswith("django."):          # never introspect Django's own commands
            continue
        if name == "run_scheduler_tick":       # the tick is the scheduler, not a job on it —
            continue                           # discovering itself would be infinite recursion
        try:
            cmd = load_command_class(app, name)
        except Exception:                       # a broken unrelated command must not stop the tick
            continue
        if getattr(cmd, "periodic", False):
            found.append((name, getattr(cmd, "periodic_kwargs", {}) or {}))
    return found


class Command(BaseCommand):
    help = "Run one tick of the background scheduler (every command marked periodic)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="List what would run, and run nothing.")

    def handle(self, *args, **opts):
        commands = periodic_commands()
        if not commands:
            self.stdout.write(self.style.WARNING("No periodic commands found — nothing to do."))
            return

        if opts["dry_run"]:
            self.stdout.write("This tick would run:")
            for name, kwargs in commands:
                self.stdout.write(f"  {name} {kwargs or ''}".rstrip())
            return

        for name, kwargs in commands:
            started = time.monotonic()
            try:
                call_command(name, **kwargs)
            except Exception as exc:
                # One command failing must not stop the others. A failed refresh should not also
                # mean no reports go out. This mirrors the `|| true` the old shell loop relied on,
                # but it is logged rather than swallowed.
                logger.exception("SCHEDULER: %s failed — %s", name, exc)
                self.stderr.write(self.style.ERROR(f"✗ {name}: {exc}"))
            else:
                took = time.monotonic() - started
                self.stdout.write(f"✓ {name} ({took:.1f}s)")
