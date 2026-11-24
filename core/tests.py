"""Branding endpoint, reporting settings (JWT_REFRESH_HOURS, DIS_PROFILE, ...)
and the fail-closed audit helper."""
import json
import os
import subprocess
import sys
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from config.settings.base import jwt_refresh_lifetime
from core.audit import EXPORT, AuditWriteError, record_audit_strict
from core.models import AuditLog
from core.testing import REPORTING_DEFAULTS


@override_settings(**REPORTING_DEFAULTS)
class BrandingEndpointTests(TestCase):
    def test_defaults_are_public_and_blank(self):
        # Blank = not set: the SPA falls back to its build-time / built-in names,
        # so a deployment that configures nothing is not renamed on upgrade.
        resp = APIClient().get("/api/branding/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "product_name": "", "short_name": "", "tagline": "", "footer": "",
            "profile": "full",
        })

    def test_a_stale_token_does_not_break_it(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        self.assertEqual(client.get("/api/branding/").status_code, 200)

    @override_settings(DIS_BRAND_PRODUCT_NAME="Digital Landscape platform",
                       DIS_BRAND_SHORT_NAME="DL", DIS_BRAND_TAGLINE="",
                       DIS_BRAND_FOOTER="Confidential", DIS_PROFILE="reporting")
    def test_values_are_returned_as_set(self):
        self.assertEqual(APIClient().get("/api/branding/").json(), {
            "product_name": "Digital Landscape platform", "short_name": "DL",
            "tagline": "", "footer": "Confidential", "profile": "reporting",
        })

    def test_server_side_names_fall_back_to_the_lab_wording(self):
        from core.branding import LAB_PRODUCT_NAME, footer, product_name
        self.assertEqual(LAB_PRODUCT_NAME, "Data Science Engine")
        self.assertEqual(product_name(), LAB_PRODUCT_NAME)
        self.assertEqual(footer(), "")
        with override_settings(DIS_BRAND_PRODUCT_NAME="  ", DIS_BRAND_FOOTER=" Internal "):
            self.assertEqual(product_name(), LAB_PRODUCT_NAME)  # whitespace = blank
            self.assertEqual(footer(), "Internal")
        with override_settings(DIS_BRAND_PRODUCT_NAME="Digital Landscape platform"):
            self.assertEqual(product_name(), "Digital Landscape platform")


def _settings_probe(env_overrides, expression):
    """Import the dev settings in a fresh interpreter with ONLY the given
    environment — the developer's .env is switched off, so the probe sees the
    declared defaults, not whatever this machine is configured with."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("DIS_") and k != "JWT_REFRESH_HOURS"}
    env.update(DJANGO_SETTINGS_MODULE="config.settings.dev", **env_overrides)
    code = ("import decouple; decouple.config = decouple.Config(decouple.RepositoryEmpty()); "
            f"from django.conf import settings as s; print({expression})")
    return subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env=env,
                          capture_output=True, text=True, timeout=120)


class ReportingSettingsTests(SimpleTestCase):
    def test_refresh_lifetime_helper(self):
        self.assertEqual(jwt_refresh_lifetime(None, 7), timedelta(days=7))
        self.assertEqual(jwt_refresh_lifetime(12, 7), timedelta(hours=12))

    def test_declared_defaults_keep_existing_behaviour(self):
        keys = sorted(REPORTING_DEFAULTS)
        probe = _settings_probe(
            {}, "__import__('json').dumps({**{k: getattr(s, k) for k in %r}, "
                "'refresh_seconds': s.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds()})"
                % keys)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        values = json.loads(probe.stdout)
        self.assertEqual(values.pop("refresh_seconds"), timedelta(days=7).total_seconds())
        self.assertEqual(values, REPORTING_DEFAULTS)

    def test_environment_overrides(self):
        probe = _settings_probe(
            {"JWT_REFRESH_HOURS": "12", "DIS_REPORT_SEND_HOUR": "6",
             "DIS_PROFILE": "Reporting"},
            "s.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(), "
            "s.DIS_REPORT_SEND_HOUR, s.DIS_PROFILE")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.split(), ["43200.0", "6", "reporting"])

    def test_invalid_values_fail_at_startup(self):
        for env, needle in (({"DIS_PROFILE": "everything"}, "DIS_PROFILE"),
                            ({"DIS_REPORT_TZ": "Mars/Olympus"}, "DIS_REPORT_TZ"),
                            ({"DIS_REPORT_SEND_HOUR": "25"}, "DIS_REPORT_SEND_HOUR")):
            probe = _settings_probe(env, "s.DIS_PROFILE")
            self.assertNotEqual(probe.returncode, 0, env)
            self.assertIn(needle, probe.stderr)


class StrictAuditTests(TestCase):
    def test_writes_and_returns_the_row(self):
        user = get_user_model().objects.create_user("aud", password="x")
        row = record_audit_strict(EXPORT, user=user, target_type="Dataset", target_id=7,
                                  summary="s" * 400, detail={"format": "pdf"})
        row.refresh_from_db()
        self.assertEqual((row.action, row.target_id, row.user), ("EXPORT", "7", user))
        self.assertEqual(len(row.summary), 255)

    def test_raises_instead_of_swallowing(self):
        with mock.patch.object(AuditLog, "save", side_effect=DatabaseError("boom")), \
                self.assertLogs("core.audit", level="ERROR"), \
                self.assertRaises(AuditWriteError):
            record_audit_strict(EXPORT, target_type="Dataset", target_id=1)
        self.assertFalse(AuditLog.objects.exists())

    def test_exports_are_filterable_in_the_audit_api(self):
        admin = get_user_model().objects.create_superuser("aud-admin", "a@x.co", "x")
        record_audit_strict(EXPORT, user=admin, target_type="Dataset", target_id=1)
        client = APIClient()
        client.force_authenticate(admin)
        body = client.get("/api/audit-logs/?action=EXPORT").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["action"], "EXPORT")


class ClientExportAuditTests(TestCase):
    """POST /api/audit/client-export/: the browser records a file BEFORE it builds it."""

    URL = "/api/audit/client-export/"

    def setUp(self):
        self.user = get_user_model().objects.create_user("viewer", "v@x.co", "x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_commits_an_export_row_before_the_browser_builds_the_file(self):
        resp = self.client.post(self.URL, {
            "format": "PDF", "filename": "Executive Overview.pdf", "target_type": "Dashboard",
            "target_id": "1", "page": "/dashboards/1", "rows": None}, format="json")
        self.assertEqual(resp.status_code, 201)
        row = AuditLog.objects.get(pk=resp.json()["audit_id"])
        self.assertEqual((row.action, row.user, row.target_type, row.target_id),
                         (EXPORT, self.user, "Dashboard", "1"))
        self.assertEqual(row.summary, "Exported 'Executive Overview.pdf' as pdf from the browser")
        self.assertEqual(row.detail, {"channel": "browser", "format": "pdf",
                                      "filename": "Executive Overview.pdf",
                                      "page": "/dashboards/1", "rows": None})

        resp = self.client.post(self.URL, {"format": "csv", "filename": "w.csv",
                                           "target_type": "Dataset", "rows": 12}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertIn("(12 rows)", AuditLog.objects.get(pk=resp.json()["audit_id"]).summary)

    def test_rejects_unknown_formats_targets_and_anonymous_callers(self):
        self.assertEqual(self.client.post(self.URL, {"format": "docx"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.URL, {"format": "csv", "target_type": "User"},
                                          format="json").status_code, 400)
        self.assertEqual(APIClient().post(self.URL, {"format": "csv"}, format="json").status_code, 401)
        self.assertFalse(AuditLog.objects.filter(action=EXPORT).exists())

    def test_an_unwritable_audit_row_refuses_the_export(self):
        with mock.patch("core.models.AuditLog.objects.create", side_effect=DatabaseError("disk full")):
            resp = self.client.post(self.URL, {"format": "png", "filename": "c.png"}, format="json")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "Export not audited; nothing was exported.")
