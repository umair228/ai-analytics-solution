"""refresh_datasets and promote_source management commands."""
import os
import shutil
import sqlite3
import tempfile
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import TestCase

from connections.models import DataSource
from core.models import AuditLog
from datasets.models import Dataset
from querybuilder.models import QueryDefinition

User = get_user_model()


def make_db(path, values):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    conn.commit()
    conn.close()


class CommandTestBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp()
        cls.db_a = os.path.join(cls.tmp, "rpt_a.sqlite")   # yesterday's restore
        cls.db_b = os.path.join(cls.tmp, "rpt_b.sqlite")   # tonight's restore
        cls.db_other = os.path.join(cls.tmp, "other.sqlite")
        make_db(cls.db_a, [1, 2])
        make_db(cls.db_b, [1, 2, 3, 4, 5])
        make_db(cls.db_other, [9])

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.user = User.objects.create_user("cmd-owner", password="x", role="analyst")
        # SQLite reads its file from database_name when options.path is unset.
        self.rpt = DataSource.objects.create(name="Rpt", source_type="sqlite",
                                             owner=self.user, database_name=self.db_a)
        self.other = DataSource.objects.create(name="Other", source_type="sqlite",
                                               owner=self.user, database_name=self.db_other)
        self.rpt_ds = self.dataset("Rpt rows", self.rpt, "SELECT v FROM t ORDER BY v")
        self.other_ds = self.dataset("Other rows", self.other, "SELECT v FROM t")

    def dataset(self, name, source, sql):
        query = QueryDefinition.objects.create(name=name, datasource=source,
                                               mode=QueryDefinition.Mode.RAW,
                                               raw_sql=sql, owner=self.user)
        return Dataset.objects.create(name=name, query=query, owner=self.user)

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command(*args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()


class RefreshDatasetsTests(CommandTestBase):
    def test_refreshes_only_the_named_sources_datasets(self):
        out, _ = self.run_command("refresh_datasets", "--datasource", "Rpt")
        self.rpt_ds.refresh_from_db()
        self.other_ds.refresh_from_db()
        self.assertEqual(self.rpt_ds.row_count, 2)
        self.assertIsNone(self.other_ds.last_refreshed_at)
        self.assertRegex(out, r"OK +Rpt rows: 2 rows \(\d+ ms\)")
        self.assertIn("Refreshed 1 dataset(s)", out)
        self.assertIn("1 ok, 0 error(s)", out)

    def test_all_and_repeatable_datasource(self):
        out, _ = self.run_command("refresh_datasets", "--all")
        self.assertIn("2 ok, 0 error(s)", out)
        out, _ = self.run_command("refresh_datasets", "--datasource", "Rpt",
                                  "--datasource", "Other")
        self.assertIn("2 ok, 0 error(s)", out)

    def test_selection_is_required_and_validated(self):
        with self.assertRaisesMessage(CommandError, "--datasource NAME"):
            self.run_command("refresh_datasets")
        with self.assertRaisesMessage(CommandError, "not both"):
            self.run_command("refresh_datasets", "--all", "--datasource", "Rpt")
        with self.assertRaisesMessage(CommandError, "Unknown data source 'Nope'"):
            self.run_command("refresh_datasets", "--datasource", "Nope")
        DataSource.objects.create(name="Rpt", source_type="sqlite", owner=self.user,
                                  database_name=self.db_b)
        with self.assertRaisesMessage(CommandError, "ambiguous"):
            self.run_command("refresh_datasets", "--datasource", "Rpt")

    def test_failures_are_reported_and_exit_non_zero(self):
        broken = self.dataset("Broken", self.rpt, "SELECT v FROM missing_table")
        out, err = StringIO(), StringIO()
        with self.assertRaisesMessage(CommandError, "1 of 2 dataset refresh(es) failed"):
            call_command("refresh_datasets", "--datasource", "Rpt", stdout=out, stderr=err)
        self.assertIn("ERROR Broken:", err.getvalue())
        self.assertIn("1 ok, 1 error(s)", out.getvalue())
        broken.refresh_from_db()
        self.assertIn("missing_table", broken.last_error)
        self.rpt_ds.refresh_from_db()
        self.assertEqual(self.rpt_ds.row_count, 2)  # the batch carried on


class PromoteSourceTests(CommandTestBase):
    def test_promote_switches_database_audits_and_refreshes(self):
        out, _ = self.run_command("promote_source", "--datasource", "Rpt",
                                  "--database", self.db_b, "--restore-id", "R-20261123",
                                  "--refresh")
        self.rpt.refresh_from_db()
        self.assertEqual(self.rpt.database_name, self.db_b)
        self.assertEqual(self.rpt.options["restore_id"], "R-20261123")
        self.assertEqual(self.rpt.options["previous_database"], self.db_a)
        self.assertIn("promoted_at", self.rpt.options)
        audit = AuditLog.objects.get(action=AuditLog.Action.UPDATE, target_type="DataSource")
        self.assertEqual(audit.target_id, str(self.rpt.pk))
        self.assertEqual(audit.detail["database"], self.db_b)
        self.assertEqual(audit.detail["previous_database"], self.db_a)
        self.assertEqual(audit.detail["restore_id"], "R-20261123")
        self.rpt_ds.refresh_from_db()
        self.assertEqual(self.rpt_ds.row_count, 5)  # read from tonight's restore
        self.other_ds.refresh_from_db()
        self.assertIsNone(self.other_ds.last_refreshed_at)  # other sources untouched
        self.assertIn(f"Promoted 'Rpt' (#{self.rpt.pk})", out)
        self.assertIn("1 ok, 0 error(s)", out)

    def test_several_sources_and_sqlite_options_path(self):
        self.other.options = {"path": self.db_other}
        self.other.save()
        self.run_command("promote_source", "--datasource", "Rpt", "--datasource", "Other",
                         "--database", self.db_b, "--refresh")
        self.other.refresh_from_db()
        self.assertEqual(self.other.options["path"], self.db_b)
        self.other_ds.refresh_from_db()
        self.assertEqual(self.other_ds.row_count, 5)
        self.assertEqual(AuditLog.objects.filter(target_type="DataSource").count(), 2)

    def test_promote_without_restore_id_drops_a_stale_one(self):
        self.rpt.options = {"restore_id": "old"}
        self.rpt.save()
        self.run_command("promote_source", "--datasource", "Rpt", "--database", self.db_b)
        self.rpt.refresh_from_db()
        self.assertNotIn("restore_id", self.rpt.options)
        self.rpt_ds.refresh_from_db()
        self.assertIsNone(self.rpt_ds.last_refreshed_at)  # no --refresh, no refresh

    def test_audit_failure_rolls_the_promotion_back(self):
        with mock.patch.object(AuditLog, "save", side_effect=DatabaseError("db down")), \
                self.assertLogs("core.audit", level="ERROR"), \
                self.assertRaisesMessage(CommandError, "Nothing was promoted"):
            self.run_command("promote_source", "--datasource", "Rpt",
                             "--database", self.db_b)
        self.rpt.refresh_from_db()
        self.assertEqual(self.rpt.database_name, self.db_a)
        self.assertNotIn("promoted_at", self.rpt.options)

    def test_unknown_source_and_refresh_failures(self):
        with self.assertRaisesMessage(CommandError, "Unknown data source 'Nope'"):
            self.run_command("promote_source", "--datasource", "Nope",
                             "--database", self.db_b)
        self.dataset("Broken", self.rpt, "SELECT v FROM missing_table")
        with self.assertRaisesMessage(CommandError, "Promotion applied, but 1 of 2"):
            self.run_command("promote_source", "--datasource", "Rpt",
                             "--database", self.db_b, "--refresh")
        self.rpt.refresh_from_db()
        self.assertEqual(self.rpt.database_name, self.db_b)  # the promotion stands

    def test_queries_pinned_to_a_database_are_listed(self):
        QueryDefinition.objects.create(name="Pinned", datasource=self.rpt,
                                       mode=QueryDefinition.Mode.RAW, raw_sql="SELECT 1",
                                       owner=self.user, database="legacy_db")
        _, err = self.run_command("promote_source", "--datasource", "Rpt",
                                  "--database", self.db_b)
        self.assertIn("Pinned -> legacy_db", err)
