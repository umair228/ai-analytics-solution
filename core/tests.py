"""Audit-trail controls.

These are compliance controls, not features, so they are tested as controls: the
question each one answers is "how do you know?", and the answer has to be a test
an assessor can read. SECURITY.md points here.
"""
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings

from core.admin import AuditLogAdmin
from core.audit import log_audit, record_audit
from core.models import AuditLog


class AuditLogIsAppendOnlyTests(TestCase):
    """The trail must be readable and nothing else — including by staff.

    Without the delete override, a staff user could bulk-remove the very records
    that prove what they did, and SECURITY.md's "append-only" claim would be
    false in the admin even while true in the API.
    """

    def setUp(self):
        self.admin = AuditLogAdmin(AuditLog, AdminSite())
        self.request = RequestFactory().get("/admin/")
        self.request.user = get_user_model().objects.create_superuser(
            username="root", password="x", email="root@example.com")

    def test_superuser_cannot_add(self):
        self.assertFalse(self.admin.has_add_permission(self.request))

    def test_superuser_cannot_change(self):
        self.assertFalse(self.admin.has_change_permission(self.request))

    def test_superuser_cannot_delete(self):
        entry = AuditLog.objects.create(action=AuditLog.Action.QUERY, summary="x")
        self.assertFalse(self.admin.has_delete_permission(self.request))
        self.assertFalse(self.admin.has_delete_permission(self.request, entry))


class AuditFailsClosedTests(TestCase):
    """A silent audit failure is worse than none: the gap is indistinguishable
    from "nothing happened". The write must take the request down with it."""

    def test_write_failure_raises_by_default(self):
        with mock.patch.object(AuditLog.objects, "create", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                log_audit(action=AuditLog.Action.QUERY, summary="should not be swallowed")

    @override_settings(AUDIT_FAIL_OPEN=True)
    def test_escape_hatch_downgrades_to_best_effort(self):
        with mock.patch.object(AuditLog.objects, "create", side_effect=RuntimeError("db down")):
            self.assertIsNone(log_audit(action=AuditLog.Action.QUERY, summary="tolerated"))


class AuditInputsAreSanitisedTests(TestCase):
    """Failing closed is only safe if the inputs can't manufacture a failure.

    Each case below is a way a caller (or an attacker upstream of us) could once
    have made an audit entry vanish.
    """

    def test_malformed_forwarded_for_does_not_suppress_the_entry(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="not-an-ip; drop table")
        request.user = get_user_model().objects.create_user(username="u1", password="x")
        entry = record_audit(request, AuditLog.Action.QUERY, summary="ran a query")
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.ip_address)

    def test_real_forwarded_for_is_kept(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1")
        request.user = get_user_model().objects.create_user(username="u2", password="x")
        self.assertEqual(record_audit(request, AuditLog.Action.LOGIN).ip_address, "203.0.113.7")

    def test_unserialisable_detail_degrades_instead_of_raising(self):
        entry = log_audit(action=AuditLog.Action.QUERY, detail={"obj": object()})
        self.assertIsNotNone(entry)
        entry.refresh_from_db()
        self.assertIn("obj", entry.detail)  # coerced to its repr, not dropped

    def test_oversized_fields_are_truncated_not_rejected(self):
        entry = log_audit(
            action=AuditLog.Action.QUERY,
            target_type="t" * 200, target_id="i" * 200, summary="s" * 900,
        )
        entry.refresh_from_db()
        self.assertEqual(len(entry.target_type), 80)
        self.assertEqual(len(entry.target_id), 80)
        self.assertEqual(len(entry.summary), 255)

    def test_anonymous_user_is_recorded_as_null_not_dropped(self):
        request = RequestFactory().get("/")
        request.user = mock.Mock(is_authenticated=False)
        entry = record_audit(request, AuditLog.Action.LOGIN, summary="anon")
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.user)
