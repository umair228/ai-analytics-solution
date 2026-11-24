"""The board report: a board drawn as a PDF on the server, downloaded (audited) or
emailed on a schedule, never for a private board."""
import io
import os
import sqlite3
import tempfile
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from pypdf import PdfReader
from rest_framework.test import APIClient

from connections.models import DataSource
from core.audit import AuditWriteError
from core.board_render import category_label, delta_for, format_value, plan
from core.models import AuditLog
from core.testing import REPORTING_DEFAULTS
from dashboards.models import Dashboard, Widget
from datasets.models import Dataset, DatasetReport
from querybuilder.models import QueryDefinition

User = get_user_model()


class NumbersReadAsOnTheBoardTests(SimpleTestCase):
    """formatValue / deltaFor (ManagementWidgets.jsx), so paper and screen agree."""

    def test_values(self):
        self.assertEqual(format_value(1968), "1,968")
        self.assertEqual(format_value(28.44, "%"), "28.4%")
        self.assertEqual(format_value(21, "d"), "21 d")
        self.assertEqual(format_value(0.8413, "ratio"), "0.84")
        self.assertEqual(format_value(1_234_567, "SAR"), "SAR 1.2M")
        self.assertEqual(format_value(None), "—")

    def test_labels_and_change(self):
        self.assertEqual(category_label("2026-06-01", "month"), "Jun 2026")
        self.assertEqual(category_label("2026-06-29"), "29 Jun 2026")
        self.assertEqual(category_label("2419"), "2419")
        from core.board_render import table_cell
        self.assertEqual(table_cell("2026-06-07", "first_entry"), "7 Jun 2026")
        self.assertEqual(table_cell(102184, "records"), "102,184")
        self.assertEqual(table_cell(2026, "year"), "2026")
        d = delta_for([5, 7, 9], "higher_better")
        self.assertEqual((d["diff"], round(d["pct"]), d["sense"]), (2, 29, "good"))
        self.assertIsNone(delta_for([9], "higher_better"))

    def test_sections_start_rows_and_rows_hold_twelve_columns(self):
        W = lambda t, x, y, w, h=4: Widget(widget_type=t, x=x, y=y, w=w, h=h, title=t)  # noqa: E731
        layout = plan([W("kpi", 6, 2, 6), W("text", 0, 0, 12, 2), W("kpi", 0, 2, 6),
                       W("table", 0, 6, 12, 8), W("bar", 0, 14, 8)])
        self.assertEqual([k for k, _ in layout], ["section", "row", "row", "row"])
        self.assertEqual([len(v) for k, v in layout if k == "row"], [2, 1, 1])


@override_settings(**REPORTING_DEFAULTS)
class BoardReportTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        con = sqlite3.connect(cls.db_path)
        con.executescript("""
            CREATE TABLE kpi (value REAL, caveat TEXT);
            INSERT INTO kpi VALUES (1968, 'Target dates are placeholders.');
            CREATE TABLE monthly (month TEXT, rfqs INTEGER);
            INSERT INTO monthly VALUES ('2026-08-01', 5), ('2026-09-01', 7),
                                       ('2026-10-01', 9), ('2026-11-01', 2);
            CREATE TABLE overdue (project_number TEXT, project_name TEXT, docs INTEGER);
            INSERT INTO overdue VALUES ('2419', 'Pressure vessel package', 261),
                                       ('2418', 'HDPE units', 260), ('2417', 'Column', 172);
            CREATE TABLE hours (project_number TEXT, spent REAL, budget REAL);
            INSERT INTO hours VALUES ('2429-2', 60888, 134058);
            CREATE TABLE register (doc TEXT, days INTEGER);
            CREATE TABLE monthly_mix (month TEXT, "External" INTEGER, "Internal" INTEGER);
            INSERT INTO monthly_mix VALUES ('2026-09-01', 40, 20), ('2026-10-01', 35, 30);
        """)
        con.executemany("INSERT INTO register VALUES (?, ?)",
                        [(f"DOC-{i:03d}", 100 - i) for i in range(15)])
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.unlink(cls.db_path)

    def setUp(self):
        self.owner = User.objects.create_user("board-owner", password="x", role="analyst")
        self.viewer = User.objects.create_user("board-viewer", password="x", role="viewer")
        self.other = User.objects.create_user("someone-else", password="x", role="analyst")
        self.source = DataSource.objects.create(
            name="board-src", source_type="sqlite", owner=self.owner,
            options={"path": self.db_path}, visibility="org")
        self.board = Dashboard.objects.create(
            name="Executive Overview", description="What management needs to know.",
            owner=self.owner, visibility="org")
        y = 0

        def ds(name, sql, owner=None, visibility="org"):
            q = QueryDefinition.objects.create(
                name=name, datasource=self.source, mode=QueryDefinition.Mode.RAW,
                owner=owner or self.owner, raw_sql=sql, visibility=visibility)
            return Dataset.objects.create(name=name, query=q, owner=owner or self.owner,
                                          visibility=visibility)

        def widget(kind, title, dataset=None, config=None, w=6, h=4):
            nonlocal y
            Widget.objects.create(dashboard=self.board, widget_type=kind, title=title,
                                  dataset=dataset, config=config or {}, x=0, y=y, w=w, h=h)
            y += h

        widget("text", "Engineering delivery", config={"content": "Are we delivering?"}, w=12, h=2)
        widget("kpi", "Overdue", ds("kpi", "SELECT value, caveat FROM kpi"),
               {"value_field": "value", "label": "Overdue documents",
                "caption": "Past their date and not done."})
        widget("kpi", "New", ds("monthly", "SELECT month, rfqs FROM monthly ORDER BY month"),
               {"value_field": "rfqs", "trend_field": "rfqs", "value_from": "last_complete",
                "direction": "higher_better", "label": "Enquiries, last full month"})
        widget("bar_list", "Projects with the most overdue documents",
               ds("overdue", "SELECT project_number, project_name, docs FROM overdue"),
               {"category_field": "project_number", "value_field": "docs",
                "name_field": "project_name", "limit": 2})
        widget("progress", "Hours against budget",
               ds("hours", "SELECT project_number, spent, budget FROM hours"),
               {"category_field": "project_number", "value_field": "spent",
                "target_field": "budget", "unit": "h", "direction": "lower_better"})
        widget("table", "Register", ds("register", "SELECT doc, days FROM register ORDER BY days DESC"),
               {"page_size": 5, "columns": [{"field": "doc", "label": "Document"},
                                            {"field": "days", "label": "Days"}]}, w=12, h=8)
        widget("bar", "Transmittals", ds("mix", 'SELECT month, "External", "Internal" FROM monthly_mix'),
               {"category_field": "month", "category_format": "month", "stacked": True,
                "value_fields": ["External", "Internal"], "rollup": "none"}, w=12, h=8)
        widget("line", "Trend", ds("trend", "SELECT month, rfqs FROM monthly ORDER BY month"),
               {"category_field": "month", "value_field": "rfqs", "rollup": "none"}, w=12, h=8)
        widget("kpi", "Secret", ds("secret", "SELECT value FROM kpi", owner=self.other,
                                   visibility="private"), {"value_field": "value"})
        widget("kpi", "Broken", ds("broken", "SELECT value FROM no_such_table"),
               {"value_field": "value", "label": "Broken figure"})

    def text_of(self, content):
        return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)

    def test_the_board_draws_as_a_pdf_with_its_figures(self):
        from dashboards.board_report import board_report_pdf

        out, filename = board_report_pdf(self.board, user=self.owner, generated_by="test")
        self.assertTrue(out.content.startswith(b"%PDF"))
        self.assertTrue(filename.startswith("executive-overview-") and filename.endswith(".pdf"))
        text = self.text_of(out.content)
        for expected in ("Executive Overview", "Engineering delivery", "Are we delivering?",
                         "1,968", "Past their date and not done.",
                         "Enquiries, last full month", "+2 (+29%)",       # 9 vs 7, not 2
                         "2419 · Pressure vessel package", "and 1 more",
                         "60,888 h of 134,058 h (45%)", "Showing 5 of 15",
                         "Transmittals", "Page 1 of"):
            self.assertIn(expected, text)
        # A figure that cannot be shown says so; it never reads as zero.
        self.assertIn("Not available to this report's owner.", text)
        self.assertIn("This figure could not be computed.", text)
        self.assertEqual(out.unavailable, 2)

    def test_download_is_audited_and_fails_closed(self):
        api = APIClient()
        api.force_authenticate(self.viewer)
        resp = api.get(f"/api/dashboards/{self.board.id}/report-pdf/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        row = AuditLog.objects.filter(action="EXPORT", target_type="Dashboard").get()
        self.assertEqual(row.target_id, str(self.board.id))
        self.assertEqual(row.detail["format"], "pdf")
        with mock.patch("core.audit.record_audit_strict", side_effect=AuditWriteError("down")):
            resp = api.get(f"/api/dashboards/{self.board.id}/report-pdf/")
        self.assertEqual(resp.status_code, 500)
        self.assertNotEqual(resp.get("Content-Type"), "application/pdf")

    def test_a_private_board_is_not_downloadable_by_others(self):
        self.board.visibility = "private"
        self.board.save()
        api = APIClient()
        api.force_authenticate(self.viewer)
        self.assertEqual(api.get(f"/api/dashboards/{self.board.id}/report-pdf/").status_code, 404)

    def test_only_a_board_everyone_may_open_can_be_scheduled(self):
        api = APIClient()
        api.force_authenticate(self.owner)
        body = {"kind": "board", "dashboard": self.board.id, "name": "Weekly board",
                "recipient_emails": "pm@example.com", "schedule": "weekly"}
        resp = api.post("/api/reports/", body, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["export_format"], "pdf")
        self.assertEqual(resp.json()["dashboard_name"], "Executive Overview")
        self.board.visibility = "private"
        self.board.save()
        resp = api.post("/api/reports/", {**body, "name": "Private board"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("shared by link only", str(resp.content))

    def test_a_one_record_board_is_not_emailed_whole(self):
        self.board.filters = [{"id": "project", "type": "picker", "required": True}]
        self.board.save()
        api = APIClient()
        api.force_authenticate(self.owner)
        resp = api.post("/api/reports/", {"kind": "board", "dashboard": self.board.id,
                                          "name": "x", "recipient_emails": "a@b.co",
                                          "schedule": "weekly"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("one record at a time", str(resp.content))
        self.assertEqual(api.get(f"/api/dashboards/{self.board.id}/report-pdf/").status_code, 400)

    def send(self):
        out, err = StringIO(), StringIO()
        call_command("send_scheduled_reports", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    @override_settings(DIS_APP_URL="https://reporting.example.com")
    def test_the_board_is_emailed_with_one_audit_row_per_recipient(self):
        DatasetReport.objects.create(
            name="Weekly board", kind=DatasetReport.Kind.BOARD, dashboard=self.board,
            owner=self.owner, recipient_emails="pm@example.com, ceo@example.com",
            schedule="weekly", export_format="pdf")
        self.send()
        self.assertEqual(len(mail.outbox), 2)
        message = mail.outbox[0]
        name, content, mimetype = message.attachments[0]
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(name.startswith("executive-overview-"))
        self.assertIn("Executive Overview", self.text_of(content))
        self.assertIn(f"https://reporting.example.com/dashboards/{self.board.id}", message.body)
        self.assertIn("2 figure(s) could not be shown", message.body)
        sends = AuditLog.objects.filter(action="SEND").order_by("id")
        self.assertEqual([r.detail["recipient"] for r in sends], ["pm@example.com", "ceo@example.com"])
        self.assertTrue(all(r.detail["status"] == "sent" and r.detail["dashboard_id"] == self.board.id
                            for r in sends))

    def test_a_board_made_private_after_scheduling_is_not_sent(self):
        DatasetReport.objects.create(
            name="Weekly board", kind=DatasetReport.Kind.BOARD, dashboard=self.board,
            owner=self.owner, recipient_emails="pm@example.com", schedule="weekly",
            export_format="pdf")
        self.board.visibility = "private"
        self.board.save()
        _, err = self.send()
        self.assertEqual(mail.outbox, [])
        self.assertIn("shared by link only", err)
