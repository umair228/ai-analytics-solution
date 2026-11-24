"""Serving the validation evidence — and being careful about which half.

The Operational Qualification is the argument for trusting the system, and it should be
easy to make. The Installation Qualification is an inventory of the ways *this* deployment
is currently weak, written in plain language with remediation attached — "DIS connects to
this LIMS as 'sa', the database SUPERUSER"; "the application is serving over plaintext
HTTP". Handing that to every authenticated user, on a system that until recently would give
an account to anyone who asked, turns an honesty feature into a reconnaissance endpoint.

So the load-bearing test in this file is the boring-sounding one: a non-admin gets a 403
from the posture endpoint.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from validation import artifact
from validation.preflight import FAIL, PASS, Check

User = get_user_model()


class PostureIsForAdministratorsOnlyTests(APITestCase):
    """The whole reason this endpoint is not simply IsAuthenticated."""

    def setUp(self):
        self.admin = User.objects.create_user("admin", password="x", role=User.Role.ADMIN)
        self.analyst = User.objects.create_user("chem", password="x",
                                                role=User.Role.ANALYST)

    def test_a_non_admin_is_REFUSED_the_list_of_our_weaknesses(self):
        self.client.force_authenticate(self.analyst)
        self.assertEqual(self.client.get("/api/validation/posture/").status_code, 403)

    def test_a_lab_manager_is_refused_too(self):
        """Seniority in the laboratory is not administration of the platform."""
        mgr = User.objects.create_user("mgr", password="x", role=User.Role.LAB_MANAGER)
        self.client.force_authenticate(mgr)
        self.assertEqual(self.client.get("/api/validation/posture/").status_code, 403)

    def test_an_anonymous_caller_is_refused(self):
        self.assertIn(self.client.get("/api/validation/posture/").status_code, (401, 403))

    def test_an_admin_gets_the_posture(self):
        fake = [Check("URS-060", "TLS enforced", FAIL, "Serving over plaintext HTTP.",
                      fix="Terminate TLS at the edge."),
                Check("URS-011", "Audit writes fail closed", PASS, "Fail-closed.")]
        with mock.patch("validation.preflight.run", return_value=fake):
            self.client.force_authenticate(self.admin)
            body = self.client.get("/api/validation/posture/").json()

        self.assertEqual(body["summary"], {"passed": 1, "failed": 1, "warnings": 0,
                                           "unknown": 0})
        self.assertIn("UNKNOWN is not a pass", body["note"])

    def test_the_posture_does_not_soften_a_failure(self):
        """An IQ that reads nicely and says nothing is how a system gets certified without
        being safe. The FAIL, its detail and its fix all reach the client intact."""
        fake = [Check("URS-061", "LIMS account is read-only", FAIL,
                      "DIS connects to this LIMS as 'sa' — the database SUPERUSER.",
                      fix="Create a read-only login and rotate the password.")]
        with mock.patch("validation.preflight.run", return_value=fake):
            self.client.force_authenticate(self.admin)
            check = self.client.get("/api/validation/posture/").json()["checks"][0]

        self.assertEqual(check["status"], "FAIL")
        self.assertIn("SUPERUSER", check["detail"])
        self.assertIn("rotate", check["fix"])


class OqReportTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("chem", password="x", role=User.Role.ANALYST)
        self.client.force_authenticate(self.user)

    def test_a_build_with_no_evidence_says_so_rather_than_looking_green(self):
        """An unvalidated build and a validated one must never render the same. This is the
        failure mode that matters: a page that is empty because nothing ran, shown to a
        customer as though it means everything passed."""
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            with override_settings(DSE_OQ_REPORT_PATH=missing):
                body = self.client.get("/api/validation/oq/").json()

        self.assertFalse(body["available"])
        self.assertIn("not", body["detail"].lower())
        self.assertNotIn("oq_passed", body)

    def test_the_report_is_served_when_the_build_carries_one(self):
        report = {"oq_passed": True, "tests_executed": 195, "requirements_total": 28,
                  "build": {"git_sha": "abc1234"}, "outcomes": []}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "oq.json"
            path.write_text(json.dumps(report))
            with override_settings(DSE_OQ_REPORT_PATH=path):
                body = self.client.get("/api/validation/oq/").json()

        self.assertTrue(body["available"])
        self.assertTrue(body["oq_passed"])
        self.assertEqual(body["tests_executed"], 195)

    def test_a_corrupt_report_is_treated_as_no_report_not_as_a_pass(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "oq.json"
            path.write_text("{ this is not json")
            with override_settings(DSE_OQ_REPORT_PATH=path):
                body = self.client.get("/api/validation/oq/").json()

        self.assertFalse(body["available"])

    def test_it_needs_authentication(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get("/api/validation/oq/").status_code, (401, 403))


class TheArtifactNamesItsBuildTests(APITestCase):
    """A validation summary that does not name the thing it validated is a piece of
    paper."""

    def _evidence(self, outcomes):
        return {"outcomes": outcomes, "tests_executed": sum(o.tests_run for o in outcomes),
                "qualified": True}

    def test_it_records_which_build_it_is_about(self):
        from validation.runner import Outcome
        from validation.requirements import ALL

        with mock.patch.dict("os.environ", {"DIS_GIT_SHA": "deadbeef", "DIS_VERSION": "1.4"}):
            doc = artifact.to_dict(self._evidence(
                [Outcome(requirement=ALL[0], status="passed", tests_run=3)]))

        self.assertEqual(doc["build"]["git_sha"], "deadbeef")
        self.assertEqual(doc["build"]["version"], "1.4")

    def test_the_manual_requirements_are_not_counted_as_passes(self):
        """Six requirements no unit test can settle. An OQ that folded them into the pass
        rate would be the exact lie this all exists to avoid."""
        from validation.runner import Outcome
        from validation.requirements import ALL

        automated = next(r for r in ALL if not r.manual)
        manual = next(r for r in ALL if r.manual)
        doc = artifact.to_dict(self._evidence([
            Outcome(requirement=automated, status="passed", tests_run=4),
            Outcome(requirement=manual, status="manual"),
        ]))

        self.assertEqual(doc["requirements_total"], 2)
        self.assertEqual(doc["requirements_automated"], 1)
        self.assertEqual(doc["requirements_manual"], 1)
        self.assertIn("not a pass", doc["scope_note"])

    def test_a_failed_requirement_makes_the_OQ_fail(self):
        from validation.runner import Outcome
        from validation.requirements import ALL

        doc = artifact.to_dict(self._evidence([
            Outcome(requirement=ALL[0], status="failed", tests_run=2,
                    failures=["some.test.that.broke"]),
        ]))
        self.assertFalse(doc["oq_passed"])
        self.assertEqual(doc["requirements_failed"], 1)

    def test_matrix_rot_makes_the_OQ_fail_too(self):
        """A requirement naming a test that no longer exists has not been verified — it has
        merely stopped being checked, which is worse, because it still reads as covered."""
        from validation.runner import Outcome
        from validation.requirements import ALL

        doc = artifact.to_dict(self._evidence([
            Outcome(requirement=ALL[0], status="missing", missing=["gone.Test"]),
        ]))
        self.assertFalse(doc["oq_passed"])
