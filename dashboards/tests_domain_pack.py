"""seed_domain_pack: the example pack, idempotency, widget replacement,
sharing, dry runs and validation errors."""
import os
import shutil
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path

import yaml
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from connections.models import DataSource
from dashboards.models import Dashboard, Widget
from datasets.models import Dataset
from querybuilder.models import QueryDefinition

User = get_user_model()
EXAMPLE_DIR = Path(settings.BASE_DIR) / "packs" / "example"
EXAMPLE = EXAMPLE_DIR / "pack.yaml"


class DomainPackTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmp, "orders.sqlite")
        conn = sqlite3.connect(cls.db_path)
        conn.executescript((EXAMPLE_DIR / "orders.sql").read_text())
        conn.close()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.admin = User.objects.create_superuser("pack-admin", "a@x.co", "x")
        self.source = DataSource.objects.create(
            name="Example Source", source_type="sqlite", owner=self.admin,
            database_name=self.db_path)

    def seed(self, *args, pack=EXAMPLE):
        out, err = StringIO(), StringIO()
        call_command("seed_domain_pack", "--pack", str(pack), *args, stdout=out, stderr=err)
        return out.getvalue()

    def write_pack(self, doc, name="pack.yaml"):
        path = Path(self.tmp) / name
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        return path

    def example(self):
        return yaml.safe_load(EXAMPLE.read_text())

    def counts(self):
        return (QueryDefinition.objects.count(), Dataset.objects.count(),
                Dashboard.objects.count(), Widget.objects.count())

    # ---- the example pack ---------------------------------------------------

    def test_example_pack_installs_and_is_idempotent(self):
        out = self.seed()
        self.assertIn("Datasets: 2 created, 0 updated, 0 unchanged.", out)
        self.assertIn("Dashboards: 1 created, 0 updated, 0 unchanged.", out)
        self.assertEqual(self.counts(), (2, 2, 1, 4))
        dataset = Dataset.objects.get(name="EX-01 Orders by status")
        self.assertEqual((dataset.query.mode, dataset.query.datasource, dataset.owner,
                          dataset.visibility, dataset.refresh_interval),
                         ("raw", self.source, self.admin, "org", "daily"))
        board = Dashboard.objects.get(name="Example · Overview")
        widgets = list(board.widgets.order_by("order"))
        self.assertEqual([(w.title, w.widget_type, w.x, w.y, w.w, w.h) for w in widgets][0],
                         ("Orders", "kpi", 0, 0, 3, 4))
        self.assertEqual(widgets[0].config, {"value_field": "orders", "rollup": "sum"})
        self.assertEqual(widgets[3].dataset.name, "EX-02 Orders by region")
        ids = [w.id for w in widgets]

        out = self.seed()  # second run: nothing changes
        self.assertIn("Datasets: 0 created, 0 updated, 2 unchanged.", out)
        self.assertIn("Dashboards: 0 created, 0 updated, 1 unchanged.", out)
        self.assertEqual(self.counts(), (2, 2, 1, 4))
        self.assertEqual([w.id for w in board.widgets.order_by("order")], ids)

    def test_refresh_prints_row_counts(self):
        out = self.seed("--refresh")
        self.assertRegex(out, r"OK +EX-01 Orders by status: 3 rows")
        self.assertRegex(out, r"OK +EX-02 Orders by region: 4 rows")
        self.assertIn("2 ok, 0 error(s)", out)
        self.assertEqual(Dataset.objects.get(name="EX-02 Orders by region").row_count, 4)

    def test_rerun_restores_widgets_and_applies_pack_edits(self):
        self.seed()
        board = Dashboard.objects.get(name="Example · Overview")
        board.widgets.filter(order=0).update(title="edited in the UI", x=9)
        Widget.objects.create(dashboard=board, title="extra", widget_type="text", order=9)
        doc = self.example()
        doc["datasets"][1]["sql"] = doc["datasets"][1]["sql"].replace(
            "ORDER BY region", "ORDER BY orders DESC")
        doc["datasets"][1]["refresh"] = "weekly"
        out = self.seed(pack=self.write_pack(doc))
        self.assertIn("dashboard updated   Example · Overview (4 widgets, replaced)", out)
        self.assertIn("dataset   updated   EX-02 Orders by region", out)
        self.assertIn("dataset   unchanged EX-01 Orders by status", out)
        self.assertEqual([w.title for w in board.widgets.order_by("order")],
                         ["Orders", "Order value", "Orders by status", "Orders by region"])
        dataset = Dataset.objects.get(name="EX-02 Orders by region")
        self.assertIn("ORDER BY orders DESC", dataset.query.raw_sql)
        self.assertEqual(dataset.refresh_interval, "weekly")
        self.assertIsNone(dataset.next_refresh_at)  # due at the next scheduler tick

    def test_objects_outside_the_pack_are_untouched(self):
        mine_q = QueryDefinition.objects.create(
            name="Mine", datasource=self.source, mode="raw", raw_sql="SELECT 1 AS one",
            owner=self.admin)
        mine = Dataset.objects.create(name="My dataset", query=mine_q, owner=self.admin)
        board = Dashboard.objects.create(name="My board", owner=self.admin)
        Widget.objects.create(dashboard=board, title="mine", widget_type="table",
                              dataset=mine)
        before = (mine.updated_at, board.updated_at, mine_q.updated_at)
        self.seed()
        self.seed()
        mine.refresh_from_db()
        board.refresh_from_db()
        mine_q.refresh_from_db()
        self.assertEqual((mine.updated_at, board.updated_at, mine_q.updated_at), before)
        self.assertEqual(list(board.widgets.values_list("title", flat=True)), ["mine"])

    def test_dry_run_saves_nothing(self):
        out = self.seed("--dry-run", "--refresh")
        self.assertIn("[DRY RUN]", out)
        self.assertIn("Datasets: 2 created", out)
        self.assertIn("Dry run: nothing was saved and nothing was refreshed.", out)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    # ---- ownership and sharing ----------------------------------------------

    def test_owner_option(self):
        analyst = User.objects.create_user("pack-analyst", password="x", role="analyst")
        self.seed("--owner", "pack-analyst")
        self.assertEqual(set(Dataset.objects.values_list("owner", flat=True)), {analyst.id})
        self.assertEqual(Dashboard.objects.get().owner, analyst)
        with self.assertRaisesMessage(CommandError, "No user named 'ghost'"):
            self.seed("--owner", "ghost")

    def test_share_with_group_tracks_membership(self):
        group = Group.objects.create(name="Report viewers")
        alice = User.objects.create_user("alice", password="x", role="viewer")
        bob = User.objects.create_user("bob", password="x", role="viewer")
        group.user_set.add(alice, bob)
        finance = Group.objects.create(name="Cost cleared")
        finance.user_set.add(alice)
        doc = self.example()
        doc["share_with_group"] = "Report viewers"
        doc["datasets"][1]["share_with_group"] = "Cost cleared"  # per-object override
        path = self.write_pack(doc)
        self.seed(pack=path)
        status = Dataset.objects.get(name="EX-01 Orders by status")
        region = Dataset.objects.get(name="EX-02 Orders by region")
        self.assertEqual(set(status.shared_with.all()), {alice, bob})
        self.assertEqual(set(region.shared_with.all()), {alice})
        self.assertEqual(set(status.query.shared_with.all()), {alice, bob})
        self.assertEqual(set(Dashboard.objects.get().shared_with.all()), {alice, bob})

        group.user_set.remove(bob)  # bob leaves the group → loses access on re-run
        out = self.seed(pack=path)
        self.assertIn("dataset   updated   EX-01 Orders by status", out)
        self.assertEqual(set(status.shared_with.all()), {alice})

    # ---- validation -----------------------------------------------------------

    def assert_invalid(self, doc, *fragments):
        with self.assertRaises(CommandError) as ctx:
            self.seed(pack=self.write_pack(doc))
        message = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        self.assertEqual(self.counts(), (0, 0, 0, 0))  # nothing written
        return message

    def test_every_problem_is_reported_and_nothing_is_written(self):
        doc = self.example()
        doc["version"] = 2
        doc["colour"] = "blue"
        doc["datasources"]["restricted"] = "No Such Source"
        doc["datasets"][0]["sql"] = "DELETE FROM orders"
        doc["datasets"][0]["refresh"] = "fortnightly"
        doc["datasets"][1]["parameters"] = []          # :status no longer declared
        doc["datasets"].append({"key": "orders_by_status", "name": "Dup key",
                                "datasource": "default", "sql": "SELECT 1"})
        widgets = doc["dashboards"][0]["widgets"]
        widgets[0]["dataset"] = "nope"
        widgets[1]["type"] = "gauge"
        widgets[2]["layout"] = {"x": -1, "w": 0}
        widgets[3]["colour"] = "red"
        message = self.assert_invalid(
            doc,
            "unsupported version 2",
            "unknown key 'colour'",
            "no data source named 'No Such Source'",
            "sql: Only read-only SELECT / WITH queries are allowed",
            "refresh: must be one of manual, hourly, daily, weekly (got 'fortnightly')",
            "sql uses :status but 'parameters' does not declare it",
            "key: 'orders_by_status' is used twice",
            "dataset: 'nope' is not a dataset key of this pack",
            "type: must be one of",
            "layout.x: must be an integer >= 0",
            "layout.w: must be an integer >= 1",
        )
        self.assertIn("problems)", message)

    def test_structural_errors(self):
        self.assert_invalid({"pack": "x"}, "version: required",
                            "datasources: required")
        doc = self.example()
        doc["visibility"] = "public"
        doc["share_with_group"] = "Nobody"
        doc["dashboards"][0]["widgets"][0]["config"] = {True: 1}   # YAML `on:` key
        self.assert_invalid(doc, "visibility: must be one of private, org",
                            "no group named 'Nobody' exists",
                            "is not text — quote it")
        path = Path(self.tmp) / "broken.yaml"
        path.write_text("pack: [unclosed\n")
        with self.assertRaisesMessage(CommandError, "invalid YAML"):
            self.seed(pack=path)
        with self.assertRaisesMessage(CommandError, "Pack file not found"):
            self.seed(pack=Path(self.tmp) / "missing.yaml")

    def test_visibility_is_enforced_so_a_pack_is_private_or_org(self):
        """core.access enforces visibility; a pack sets no lab, so LAB would grant nobody."""
        doc = self.example()
        doc["visibility"] = "shared"
        self.assert_invalid(doc, "'shared' was renamed: use org")
        doc["visibility"] = "lab"
        self.assert_invalid(doc, "visibility: must be one of private, org (got 'lab')")

    def test_refuses_a_query_that_feeds_a_dataset_outside_the_pack(self):
        shared = QueryDefinition.objects.create(
            name="EX-01 Orders by status", datasource=self.source, mode="raw",
            raw_sql="SELECT 1 AS one", owner=self.admin)
        Dataset.objects.create(name="A private copy", query=shared, owner=self.admin)
        with self.assertRaisesMessage(CommandError, "also feeds 'A private copy'"):
            self.seed()
        shared.refresh_from_db()
        self.assertEqual(shared.raw_sql, "SELECT 1 AS one")

    def test_dates_in_yaml_are_stored_as_iso_text(self):
        doc = self.example()
        doc["dashboards"][0]["filters"] = [
            {"column": "ordered_at", "type": "date-range", "from": "2026-01-01"}]
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).replace(
            "'2026-01-01'", "2026-01-01")  # a bare YAML date
        path = Path(self.tmp) / "dates.yaml"
        path.write_text(text)
        self.seed(pack=path)
        self.assertEqual(Dashboard.objects.get().filters[0]["from"], "2026-01-01")

    # ---- scheduled reports ----------------------------------------------------

    def report_pack(self, **report):
        doc = self.example()
        doc["reports"] = [{"key": "weekly_status", "name": "EX weekly · Orders by status",
                           "dataset": "orders_by_status", "schedule": "weekly",
                           "format": "pdf", **report}]
        return self.write_pack(doc, name="reports.yaml")

    def test_reports_start_inactive_and_keep_the_recipients_an_admin_set(self):
        from datasets.models import DatasetReport

        pack = self.report_pack()
        out = self.seed(pack=pack)
        self.assertIn("Reports: 1 created, 0 updated, 0 unchanged.", out)
        self.assertIn("weekly, pdf, inactive: no recipients", out)
        report = DatasetReport.objects.get()
        self.assertEqual((report.dataset.name, report.schedule, report.export_format,
                          report.owner, report.recipient_emails, report.is_active),
                         ("EX-01 Orders by status", "weekly", "pdf", self.admin, "", False))

        out = self.seed("--recipients", " a@x.co, b@y.co ,a@x.co", pack=pack)
        self.assertIn("weekly, pdf, 2 recipients", out)
        report.refresh_from_db()
        self.assertEqual((report.recipient_emails, report.is_active), ("a@x.co, b@y.co", True))

        # An administrator changes the list on the Reports page; a plain re-seed keeps it.
        report.recipient_emails = "c@z.co"
        report.save()
        out = self.seed(pack=pack)
        self.assertIn("Reports: 0 created, 0 updated, 1 unchanged.", out)
        report.refresh_from_db()
        self.assertEqual((report.recipient_emails, report.is_active), ("c@z.co", True))

        # The pack still owns the schedule and format; an empty --recipients turns it off.
        out = self.seed("--recipients", "", pack=self.report_pack(schedule="monthly",
                                                                  format="xlsx"))
        self.assertIn("monthly, xlsx, inactive: no recipients", out)
        report.refresh_from_db()
        self.assertEqual((report.schedule, report.export_format, report.is_active),
                         ("monthly", "xlsx", False))

    def test_report_problems_are_reported_and_recipients_never_come_from_the_pack(self):
        doc = self.example()
        doc["reports"] = [
            {"key": "r1", "name": "R1", "dataset": "nope", "schedule": "hourly",
             "format": "docx", "recipients": ["someone@x.co"]},
            {"key": "r1", "name": "R2", "dataset": "orders_by_status", "schedule": "weekly"},
        ]
        self.assert_invalid(
            doc,
            "recipients: recipients are not part of a pack",
            "dataset: 'nope' is not a dataset key of this pack",
            "schedule: must be one of daily, weekly, monthly (got 'hourly')",
            "format: must be one of xlsx, pdf, csv (got 'docx')",
            "key: 'r1' is used twice",
        )
        self.assertNotIn("unknown key 'recipients'", str(self.seed_error(doc)))
        with self.assertRaisesMessage(CommandError, "--recipients: 1 invalid address: not-an-email"):
            self.seed("--recipients", "a@x.co,not-an-email", pack=self.report_pack())

    def seed_error(self, doc):
        with self.assertRaises(CommandError) as ctx:
            self.seed(pack=self.write_pack(doc))
        return ctx.exception
