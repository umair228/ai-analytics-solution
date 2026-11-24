"""The scheduler must not be able to silently forget a job.

Twice now, a management command that was supposed to run on a schedule was simply not named in the
production scheduler, and in both cases the symptom was silence rather than failure:

  * `run_scheduled_replication` was missing, so the replication app was inert in production and
    generated SQL ran against the live LIMS.
  * `send_scheduled_reports` was missing, so every scheduled report a user created was stored,
    marked active, shown in the UI as scheduled — and never sent.

Nothing errored. Nothing logged. The screen said the job was scheduled and the user believed it.

These tests exist so that cannot happen a third time. They check the three links in the chain:

  1. the tick discovers every command that asked to be scheduled,
  2. production actually runs the tick, and
  3. a command that clearly does scheduled work has not forgotten to ask.

The third is the one that matters. The first two would pass happily while a brand-new scheduled
feature sat unwired — so we go looking for the shape of scheduled work and demand it be declared.
"""
import pathlib
import re

from django.core.management import get_commands, load_command_class
from django.test import SimpleTestCase

from core.management.commands.run_scheduler_tick import periodic_commands

REPO = pathlib.Path(__file__).resolve().parent.parent

# The commands we know must run on the scheduler. Losing one of these from the tick is exactly the
# regression that has already shipped twice.
MUST_BE_SCHEDULED = {
    "run_scheduled_refreshes",
    "run_scheduled_replication",
    "send_scheduled_reports",
    "send_lab_brief",
}

# A command is doing scheduled work if it reads one of the "am I due yet?" fields.
DUE_LOGIC = re.compile(r"\b(last_sent_at|next_refresh_at|next_run_at|last_run_at)\b")

# Commands that touch a due-field but are operator tools, not scheduled jobs. Each needs a reason.
NOT_SCHEDULED = {
    # Exports the golden set for evaluation. Run by a human, never on a timer.
    "export_golden",
    # The tick IS the scheduler. It names the due-fields in its own documentation, which is why it
    # trips the scan below. It must never be marked periodic: it would discover itself and recurse.
    "run_scheduler_tick",
}


class SchedulerTickTests(SimpleTestCase):

    def test_tick_discovers_every_command_that_must_be_scheduled(self):
        discovered = {name for name, _ in periodic_commands()}
        missing = MUST_BE_SCHEDULED - discovered
        self.assertFalse(
            missing,
            f"These commands must run on the scheduler but the tick would not run them: "
            f"{sorted(missing)}. Set `periodic = True` on the Command class. If a job is not in "
            f"the tick it does not run, and nothing will tell you.",
        )

    def test_a_deployment_can_switch_a_job_off_and_it_is_logged(self):
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command
        from django.test import override_settings

        with override_settings(DIS_SCHEDULER_SKIP=["send_lab_brief", "no_such_job"]), \
                mock.patch("core.management.commands.run_scheduler_tick.call_command") as run:
            out, err = StringIO(), StringIO()
            call_command("run_scheduler_tick", stdout=out, stderr=err)
        ran = {c.args[0] for c in run.call_args_list}
        self.assertNotIn("send_lab_brief", ran)
        self.assertIn("send_scheduled_reports", ran)
        self.assertIn("- send_lab_brief (skipped: DIS_SCHEDULER_SKIP)", out.getvalue())
        self.assertIn("names no periodic command: no_such_job", err.getvalue())

    def test_production_scheduler_invokes_the_tick(self):
        """The compose file must call the tick — not name commands by hand, which is how they got
        forgotten. Skipped when the deploy repo is not checked out alongside this one."""
        compose = REPO.parent / "dis-deploy" / "docker-compose.prod.yml"
        if not compose.exists():
            self.skipTest("dis-deploy not checked out alongside the backend")

        text = compose.read_text()
        scheduler = text.split("scheduler:", 1)[-1].split("\n  web:", 1)[0]
        self.assertIn(
            "run_scheduler_tick", scheduler,
            "The production scheduler does not run `run_scheduler_tick`. If it goes back to "
            "naming commands one by one, the next scheduled feature will be forgotten the same "
            "way replication and reports were.",
        )

    def test_a_command_with_due_logic_has_asked_to_be_scheduled(self):
        """Find the shape of scheduled work, and insist it be declared.

        This is the test that would have caught both historical bugs at the moment they were
        written, rather than in production months later.
        """
        undeclared = []
        for name, app in sorted(get_commands().items()):
            if app.startswith("django.") or name in NOT_SCHEDULED:
                continue
            try:
                cmd = load_command_class(app, name)
            except Exception:
                continue

            source = pathlib.Path(str(cmd.__class__.__module__).replace(".", "/") + ".py")
            path = REPO / source
            if not path.exists():
                continue
            if DUE_LOGIC.search(path.read_text()) and not getattr(cmd, "periodic", False):
                undeclared.append(name)

        self.assertFalse(
            undeclared,
            f"{sorted(undeclared)} check whether their work is due, which means they are meant to "
            f"run on a schedule — but they have not set `periodic = True`, so the scheduler will "
            f"never run them. The feature will appear to work and quietly do nothing. Either mark "
            f"the command periodic, or add it to NOT_SCHEDULED with a reason.",
        )
