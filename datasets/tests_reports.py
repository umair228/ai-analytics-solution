"""Scheduled email reports: export formats, per-recipient fail-closed audit,
send hour / time zone / calendar months, subject prefix."""
import datetime
import io
import os
import sqlite3
import tempfile
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings
from openpyxl import load_workbook

from analytics.models import AnalysisRun
from connections.models import DataSource
from core.models import AuditLog
from core.testing import REPORTING_DEFAULTS
from datasets.management.commands.send_scheduled_reports import is_due
from datasets.models import Dataset, DatasetReport
from querybuilder.models import QueryDefinition

User = get_user_model()
UTC = datetime.timezone.utc


def utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


@override_settings(**REPORTING_DEFAULTS)
class ScheduledReportSendTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = sqlite3.connect(cls.db_path)
        conn.execute("CREATE TABLE kpi (project TEXT, progress REAL, data_as_of TEXT)")
        conn.executemany("INSERT INTO kpi VALUES (?, ?, ?)", [
            ("P1", 41.5, "2026-11-22T21:00:00+00:00"),
            ("P2", 77.0, "2026-11-22T21:00:00+00:00"),
            ("P3", 12.25, "2026-11-22T21:00:00+00:00")])
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.unlink(cls.db_path)

    def setUp(self):
        self.owner = User.objects.create_user("rep-owner", password="x", role="analyst")
        source = DataSource.objects.create(name="rep-src", source_type="sqlite",
                                           owner=self.owner, options={"path": self.db_path})
        query = QueryDefinition.objects.create(
            name="kpi", datasource=source, mode=QueryDefinition.Mode.RAW, owner=self.owner,
            raw_sql="SELECT project, progress, data_as_of FROM kpi ORDER BY project")
        self.dataset = Dataset.objects.create(name="ENG-01 Progress", query=query,
                                              owner=self.owner)
        self.report = DatasetReport.objects.create(
            name="Weekly management pack", dataset=self.dataset, owner=self.owner,
            recipient_emails="pm@example.com, ceo@example.com", schedule="weekly",
            export_format="xlsx")

    def send(self):
        out, err = StringIO(), StringIO()
        call_command("send_scheduled_reports", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def sends(self):
        return AuditLog.objects.filter(action="SEND").order_by("id")

    def attachment(self, message):
        self.assertEqual(len(message.attachments), 1)
        return message.attachments[0]

    def test_xlsx_report_one_message_and_audit_row_per_recipient(self):
        out, _ = self.send()
        self.assertIn("Sent 1 report(s).", out)
        self.assertEqual([m.to for m in mail.outbox],
                         [["pm@example.com"], ["ceo@example.com"]])
        message = mail.outbox[0]
        self.assertEqual(message.subject, "[DIS Report] Weekly management pack")
        self.assertTrue(message.body.startswith(
            "Scheduled weekly report for dataset 'ENG-01 Progress'.\nRow count: 3\n"))
        self.assertIn("Data as of:", message.body)
        self.assertIn("Data as of: 2026-11-23 00:00 (Asia/Riyadh)", message.body)
        self.assertNotIn("\n-- \n", message.body)  # no brand configured: no signature
        name, content, mimetype = self.attachment(message)
        self.assertRegex(name, r"^eng-01-progress-\d{8}\.xlsx$")
        self.assertEqual(mimetype,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        ws = load_workbook(io.BytesIO(content)).active
        self.assertEqual(ws.max_row - (int(ws.freeze_panes[1:]) - 1), 3)

        rows = list(self.sends())
        self.assertEqual([r.detail["recipient"] for r in rows],
                         ["pm@example.com", "ceo@example.com"])
        for row in rows:
            self.assertEqual((row.target_type, row.target_id, row.user),
                             ("DatasetReport", str(self.report.pk), self.owner))
            self.assertEqual(row.detail["status"], "sent")
            self.assertEqual(row.detail["format"], "xlsx")
            self.assertEqual(row.detail["row_count"], 3)
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.last_sent_at)

        mail.outbox.clear()
        self.send()  # not due again within the week
        self.assertEqual(mail.outbox, [])

    def test_pdf_and_csv_formats_are_honoured(self):
        self.report.export_format = "pdf"
        self.report.save()
        self.send()
        name, content, mimetype = self.attachment(mail.outbox[0])
        self.assertTrue(name.endswith(".pdf"))
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))

        mail.outbox.clear()
        self.report.export_format, self.report.last_sent_at = "csv", None
        self.report.save()
        self.send()
        name, content, mimetype = self.attachment(mail.outbox[0])
        self.assertTrue(name.endswith(".csv"))
        self.assertEqual(mimetype, "text/csv")
        text = content if isinstance(content, str) else content.decode("utf-8")
        self.assertTrue(text.startswith("﻿# "))
        self.assertIn("project,progress,data_as_of", text)

    @override_settings(DIS_EMAIL_SUBJECT_PREFIX="[PMSv2 Report]",
                       DIS_BRAND_PRODUCT_NAME="Digital Landscape platform")
    def test_subject_prefix_and_brand_come_from_settings(self):
        self.send()
        self.assertEqual(mail.outbox[0].subject, "[PMSv2 Report] Weekly management pack")
        self.assertIn("Digital Landscape platform", mail.outbox[0].body)
        self.assertNotIn("DSE", mail.outbox[0].subject)

    def test_audit_failure_blocks_the_send(self):
        with mock.patch.object(AuditLog, "save", side_effect=DatabaseError("db down")), \
                self.assertLogs(level="ERROR") as logs:
            _, err = self.send()
        self.assertEqual(mail.outbox, [])
        self.assertIn("audit failed", err)
        self.assertTrue(any("could not be written" in line for line in logs.output))
        self.report.refresh_from_db()
        self.assertIsNone(self.report.last_sent_at)  # retried on the next run
        self.assertEqual(self.sends().count(), 0)

    def test_smtp_failure_is_recorded_on_the_audit_row(self):
        real_send = mail.EmailMessage.send
        calls = {"n": 0}

        def flaky(message, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("smtp down")
            return real_send(message, *args, **kwargs)

        with mock.patch.object(mail.EmailMessage, "send", flaky), \
                self.assertLogs("datasets.management.commands.send_scheduled_reports",
                                level="ERROR"):
            self.send()
        self.assertEqual([r.detail["status"] for r in self.sends()], ["sent", "failed"])
        self.assertIn("smtp down", self.sends().last().detail["error"])
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.last_sent_at)  # delivered to one recipient

    def test_unsupported_format_fails_loudly_instead_of_falling_back(self):
        self.report.export_format = "html"
        self.report.save()
        with self.assertLogs("datasets.management.commands.send_scheduled_reports",
                             level="ERROR"):
            _, err = self.send()
        self.assertIn("not supported", err)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.sends().count(), 0)

    def test_owner_who_lost_access_gets_nothing_sent(self):
        outsider = User.objects.create_user("rep-outsider", password="x", role="viewer")
        self.report.owner = outsider
        self.report.save()
        with self.assertLogs("datasets.management.commands.send_scheduled_reports",
                             level="ERROR"):
            _, err = self.send()
        self.assertIn("can no longer view", err)
        self.assertEqual(mail.outbox, [])

    def test_analysis_run_pdf_is_rendered_without_weasyprint(self):
        run = AnalysisRun.objects.create(
            name="Progress stats", workflow="statistics", method="descriptive",
            dataset=self.dataset, owner=self.owner,
            result={"columns": ["metric", "value"],
                    "rows": [{"metric": "mean", "value": 43.6}],
                    "interpretation": "Average progress is 43.6%."})
        report = DatasetReport.objects.create(
            name="Stats pack", kind=DatasetReport.Kind.ANALYSIS_RUN, analysis_run=run,
            owner=self.owner, recipient_emails="qa@example.com", export_format="pdf")
        self.report.delete()
        self.send()
        name, content, mimetype = self.attachment(mail.outbox[0])
        self.assertEqual(name, "Progress_stats.pdf")
        self.assertTrue(content.startswith(b"%PDF"))
        row = self.sends().get()
        self.assertEqual((row.target_id, row.detail["format"], row.detail["status"]),
                         (str(report.pk), "pdf", "sent"))


@override_settings(**REPORTING_DEFAULTS)
class DueTests(SimpleTestCase):
    def report(self, schedule, last_sent_at=None):
        return DatasetReport(name="r", schedule=schedule, last_sent_at=last_sent_at)

    def test_default_cadence_is_unchanged(self):
        now = utc(2026, 11, 23, 2, 0)  # 05:00 in Riyadh — no send hour by default
        self.assertTrue(is_due(self.report("daily"), now))
        self.assertFalse(is_due(self.report("daily", now - datetime.timedelta(hours=23)), now))
        self.assertTrue(is_due(self.report("daily", now - datetime.timedelta(hours=25)), now))
        self.assertFalse(is_due(self.report("weekly", now - datetime.timedelta(days=6)), now))
        self.assertTrue(is_due(self.report("weekly", now - datetime.timedelta(days=7)), now))
        self.assertFalse(is_due(self.report("monthly", now - datetime.timedelta(days=29)), now))
        self.assertTrue(is_due(self.report("monthly", now - datetime.timedelta(days=30)), now))

    @override_settings(DIS_REPORT_SEND_HOUR=6, DIS_REPORT_TZ="Asia/Riyadh")
    def test_send_hour_in_report_time_zone(self):
        self.assertFalse(is_due(self.report("daily"), utc(2026, 11, 23, 2, 30)))  # 05:30 AST
        self.assertTrue(is_due(self.report("daily"), utc(2026, 11, 23, 3, 5)))    # 06:05 AST
        # Calendar days: yesterday's 06:10 send does not hold back today's 06:05 run.
        yesterday = utc(2026, 11, 22, 3, 10)
        self.assertTrue(is_due(self.report("daily", yesterday), utc(2026, 11, 23, 3, 5)))
        self.assertFalse(is_due(self.report("daily", utc(2026, 11, 23, 3, 5)),
                                utc(2026, 11, 23, 20, 0)))
        self.assertFalse(is_due(self.report("weekly", utc(2026, 11, 17, 3, 0)),
                                utc(2026, 11, 23, 3, 5)))
        self.assertTrue(is_due(self.report("weekly", utc(2026, 11, 16, 3, 0)),
                               utc(2026, 11, 23, 3, 5)))

    @override_settings(DIS_REPORT_SEND_HOUR=6, DIS_REPORT_CALENDAR_MONTHS=True)
    def test_calendar_months(self):
        sent_nov_1 = utc(2026, 11, 1, 5, 0)  # 08:00 on 1 Nov in Riyadh
        self.assertFalse(is_due(self.report("monthly", sent_nov_1), utc(2026, 11, 30, 12, 0)))
        self.assertFalse(is_due(self.report("monthly", sent_nov_1), utc(2026, 12, 1, 2, 0)))
        self.assertTrue(is_due(self.report("monthly", sent_nov_1), utc(2026, 12, 1, 3, 30)))
        # A report last sent on 20 Oct goes out again on 1 Nov (a new calendar
        # month); the original 30-day rule would hold it until 19 Nov.
        sent_oct_20, nov_1 = utc(2026, 10, 20, 5, 0), utc(2026, 11, 1, 3, 30)
        self.assertTrue(is_due(self.report("monthly", sent_oct_20), nov_1))
        with override_settings(DIS_REPORT_CALENDAR_MONTHS=False):
            self.assertFalse(is_due(self.report("monthly", sent_oct_20), nov_1))
