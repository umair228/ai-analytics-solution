"""The tick must actually deliver — not merely run.

Wiring `send_scheduled_reports` into the scheduler is only half a fix. A scheduler that dutifully
invokes a command which then sends nothing is the same defect wearing a different hat, and it would
pass every structural test in `tests_scheduler.py`.

So this goes end to end: a real report, due, and an assertion that mail left the building.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from connections.models import DataSource
from datasets.models import Dataset, DatasetReport
from querybuilder.models import QueryDefinition

RESULT = {"columns": ["product"], "rows": [["PROPANE"]], "row_count": 1}


@mock.patch("querybuilder.executor.execute_raw_sql", return_value=RESULT)
class TickDeliversTests(TestCase):
    """The executor is stubbed: it has its own tests, and what is on trial here is whether the
    tick causes a due report to be mailed at all."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reporter", password="x")
        source = DataSource.objects.create(
            name="lims", source_type="sqlite", owner=self.user,
            options={"path": "/tmp/tick.sqlite"})
        query = QueryDefinition.objects.create(
            name="qc", datasource=source, mode=QueryDefinition.Mode.RAW,
            raw_sql="SELECT product FROM result", owner=self.user)
        self.dataset = Dataset.objects.create(
            name="QC runs", owner=self.user, query=query,
            cached_columns=["product"], cached_rows=[{"product": "PROPANE"}],
            row_count=1, total_row_count=1)

    def _report(self, **kw):
        return DatasetReport.objects.create(
            dataset=self.dataset, owner=self.user, name="Daily QC", schedule="daily",
            recipient_emails="qa@lab.local", is_active=True, **kw)

    @mock.patch("datasets.management.commands.send_scheduled_reports.EmailMessage")
    def test_the_tick_sends_a_report_that_is_due(self, email, _sql):
        report = self._report(last_sent_at=None)      # never sent → due immediately

        call_command("run_scheduler_tick")

        self.assertTrue(
            email.return_value.send.called,
            "The tick ran but the due report never went out. That is precisely the bug this "
            "change exists to fix — a report the user was told was scheduled, silently not sent.",
        )
        report.refresh_from_db()
        self.assertIsNotNone(
            report.last_sent_at,
            "The report was sent but last_sent_at was not stamped, so the next tick would send "
            "it again — every five minutes, forever.",
        )

    @mock.patch("datasets.management.commands.send_scheduled_reports.EmailMessage")
    def test_the_tick_does_not_resend_a_report_that_is_not_due(self, email, _sql):
        """The tick runs every five minutes. A daily report must not go out 288 times a day."""
        self._report(last_sent_at=timezone.now())     # just sent → not due

        call_command("run_scheduler_tick")

        self.assertFalse(
            email.return_value.send.called,
            "A report sent moments ago was sent again. Running the tick every five minutes would "
            "then mail every recipient 288 times a day.",
        )

    @mock.patch("datasets.management.commands.send_scheduled_reports.EmailMessage")
    def test_one_failing_job_does_not_stop_the_others(self, email, _sql):
        """The old shell loop isolated each command with `|| true`. The tick must do the same, or
        a broken refresh would also mean no reports go out."""
        self._report(last_sent_at=None)

        with mock.patch("datasets.management.commands.run_scheduled_refreshes."
                        "Command.handle", side_effect=RuntimeError("refresh exploded")):
            call_command("run_scheduler_tick")

        self.assertTrue(
            email.return_value.send.called,
            "A failure in one scheduled job stopped the rest of the tick. One broken job must "
            "not silence every other one.",
        )
