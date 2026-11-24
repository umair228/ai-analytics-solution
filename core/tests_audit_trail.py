"""The audit trail must say what a record USED to say — 21 CFR Part 11 §11.10(e).

    "…record changes shall not obscure previously recorded information."

It said `"Updated data source 'EGPC LIMS'"` and stopped. An assessor asks what the value
was before, and there was no answer anywhere in the system. Editing or deleting a *Dataset*
was not recorded at all.

The dangerous part of fixing this is the fix itself. The obvious implementation — diff the
model — writes ``DataSource.password`` into ``AuditLog.detail``, which is served by a REST
endpoint and copied into every nightly backup. A compliance feature that creates a
credential leak is not a compliance feature, so the first test here is the one that says
the trail never contains the secret it just recorded a change to.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from connections.models import DataSource
from core.audit import diff, snapshot
from core.models import AuditLog
from querybuilder.models import QueryDefinition

User = get_user_model()


class SecretsNeverReachTheTrailTests(TestCase):
    """The trap: a compliance feature that leaks credentials."""

    def setUp(self):
        self.admin = User.objects.create_user("admin", password="x", role=User.Role.ADMIN)
        self.ds = DataSource.objects.create(
            name="EGPC LIMS", owner=self.admin, source_type="mssql",
            host="10.0.0.5", username="sa", password="hunter2-the-real-password")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_a_snapshot_redacts_the_credential_but_still_records_that_there_is_one(self):
        snap = snapshot(self.ds)
        # `password` is a property over the encrypted column; `password_encrypted` is the
        # concrete field, and it is the one that must never surface.
        self.assertNotIn("hunter2-the-real-password", str(snap))
        self.assertNotIn(self.ds.password_encrypted, str(snap))
        self.assertTrue(snap["password_encrypted"].startswith("***("))

        # And the non-secret configuration IS captured — that is the point.
        self.assertEqual(snap["host"], "10.0.0.5")
        self.assertEqual(snap["username"], "sa")

    def test_the_marker_changes_when_the_secret_changes_and_not_otherwise(self):
        """The whole reason the redaction is not a constant `"***"`. If it were, both sides
        of the diff would compare equal and a PASSWORD CHANGE WOULD LEAVE NO ENTRY — the
        redaction would have erased the one fact it exists to record."""
        before = snapshot(self.ds)
        self.assertEqual(snapshot(self.ds)["password_encrypted"],
                         before["password_encrypted"])      # stable for the same secret

        self.ds.password = "a-completely-different-password"
        self.ds.save()
        after = snapshot(self.ds)
        self.assertNotEqual(after["password_encrypted"], before["password_encrypted"])
        self.assertTrue(diff(before, after), "the change vanished")

    def test_changing_a_password_is_recorded_WITHOUT_the_password(self):
        r = self.client.patch(f"/api/datasources/{self.ds.id}/",
                              {"password": "a-brand-new-secret"}, format="json")
        self.assertEqual(r.status_code, 200)

        entry = AuditLog.objects.filter(action=AuditLog.Action.UPDATE,
                                        target_type="DataSource").first()
        self.assertIsNotNone(entry, "the change was not audited at all")

        blob = str(entry.detail)
        self.assertNotIn("a-brand-new-secret", blob)
        self.assertNotIn("hunter2-the-real-password", blob)

        # The audit-relevant fact survives: the credential changed, and the trail says so
        # — with two different opaque markers and neither of the secrets.
        change = entry.detail["changes"]["password_encrypted"]
        self.assertTrue(change["from"].startswith("***("))
        self.assertTrue(change["to"].startswith("***("))
        self.assertNotEqual(change["from"], change["to"])

    def test_the_whole_audit_api_never_serves_a_credential(self):
        """AuditLog is exposed over REST. Whatever we put in `detail` is readable."""
        self.client.patch(f"/api/datasources/{self.ds.id}/",
                          {"host": "10.0.0.9", "password": "another-secret"},
                          format="json")
        body = str(self.client.get("/api/audit-logs/").json())
        self.assertNotIn("another-secret", body)
        self.assertNotIn("hunter2-the-real-password", body)

    def test_laboratory_data_does_not_end_up_in_the_audit_trail(self):
        """`Dataset.cached_rows` is the query's RESULT SET. Diffing it would copy patient
        or sample data into a trail that exists to record who changed the configuration."""
        from datasets.models import Dataset
        q = QueryDefinition.objects.create(name="q", datasource=self.ds, owner=self.admin,
                                           mode=QueryDefinition.Mode.RAW, raw_sql="SELECT 1")
        d = Dataset.objects.create(name="d", query=q, owner=self.admin,
                                   cached_rows=[{"patient": "SECRET-SAMPLE-042"}])
        self.assertNotIn("SECRET-SAMPLE-042", str(snapshot(d)))


class WhatItUsedToSayTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password="x", role=User.Role.ADMIN)
        self.ds = DataSource.objects.create(name="EGPC LIMS", owner=self.admin,
                                            source_type="mssql", host="10.0.0.5")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_an_update_records_the_old_value_and_the_new_one(self):
        self.client.patch(f"/api/datasources/{self.ds.id}/", {"host": "10.0.0.9"},
                          format="json")
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, target_type="DataSource")
        self.assertEqual(entry.detail["changes"]["host"],
                         {"from": "10.0.0.5", "to": "10.0.0.9"})

    def test_only_the_fields_that_changed_are_recorded(self):
        self.client.patch(f"/api/datasources/{self.ds.id}/", {"host": "10.0.0.9"},
                          format="json")
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, target_type="DataSource")
        self.assertEqual(list(entry.detail["changes"]), ["host"])

    def test_a_no_op_update_writes_no_entry(self):
        """An entry that records nothing changing is noise, and noise is what makes a trail
        unreadable — which is its own kind of obscuring."""
        self.client.patch(f"/api/datasources/{self.ds.id}/", {"host": "10.0.0.5"},
                          format="json")
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.UPDATE).count(), 0)

    def test_a_delete_keeps_the_record_that_is_about_to_vanish(self):
        """After the row is gone the trail is the only place it exists."""
        self.client.delete(f"/api/datasources/{self.ds.id}/")
        entry = AuditLog.objects.get(action=AuditLog.Action.DELETE, target_type="DataSource")
        self.assertEqual(entry.detail["deleted"]["name"], "EGPC LIMS")
        self.assertEqual(entry.detail["deleted"]["host"], "10.0.0.5")

    def test_editing_a_dataset_is_audited_at_all(self):
        """It was not. Repointing a dataset at a different query left no trace whatever."""
        from datasets.models import Dataset
        q = QueryDefinition.objects.create(name="q", datasource=self.ds, owner=self.admin,
                                           mode=QueryDefinition.Mode.RAW, raw_sql="SELECT 1")
        d = Dataset.objects.create(name="Sulphur", query=q, owner=self.admin)

        self.client.patch(f"/api/datasets/{d.id}/", {"name": "Sulphur (rev 2)"},
                          format="json")
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, target_type="Dataset")
        self.assertEqual(entry.detail["changes"]["name"],
                         {"from": "Sulphur", "to": "Sulphur (rev 2)"})

    def test_the_diff_is_symmetric_about_added_and_removed_keys(self):
        self.assertEqual(diff({"a": 1}, {"a": 2}), {"a": {"from": 1, "to": 2}})
        self.assertEqual(diff({}, {"a": 1}), {"a": {"from": None, "to": 1}})
        self.assertEqual(diff({"a": 1}, {}), {"a": {"from": 1, "to": None}})
        self.assertEqual(diff({"a": 1}, {"a": 1}), {})


class ReasonForChangeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password="x", role=User.Role.ADMIN)
        self.ds = DataSource.objects.create(name="EGPC LIMS", owner=self.admin,
                                            source_type="mssql", host="10.0.0.5")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_a_stated_reason_is_recorded(self):
        self.client.patch(f"/api/datasources/{self.ds.id}/", {"host": "10.0.0.9"},
                          format="json", HTTP_X_CHANGE_REASON="CR-2291: LIMS server moved")
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE)
        self.assertEqual(entry.reason, "CR-2291: LIMS server moved")

    def test_by_default_a_reason_is_not_demanded(self):
        """Turning it on rejects updates from any client that does not send one, so it has
        to be a deliberate act rather than a surprise upgrade."""
        r = self.client.patch(f"/api/datasources/{self.ds.id}/", {"host": "10.0.0.9"},
                              format="json")
        self.assertEqual(r.status_code, 200)

    @override_settings(DSE_REQUIRE_CHANGE_REASON=["DataSource"])
    def test_when_switched_on_a_change_without_a_reason_is_REFUSED(self):
        r = self.client.patch(f"/api/datasources/{self.ds.id}/", {"host": "10.0.0.9"},
                              format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("reason", str(r.json()).lower())
        self.ds.refresh_from_db()
        self.assertEqual(self.ds.host, "10.0.0.5", "the change went through anyway")

    @override_settings(DSE_REQUIRE_CHANGE_REASON=["DataSource"])
    def test_when_switched_on_a_change_WITH_a_reason_proceeds(self):
        r = self.client.patch(f"/api/datasources/{self.ds.id}/", {"host": "10.0.0.9"},
                              format="json", HTTP_X_CHANGE_REASON="CR-2291")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(AuditLog.objects.get(action=AuditLog.Action.UPDATE).reason,
                         "CR-2291")
