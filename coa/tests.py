"""Certificates of Analysis, and the signatures that approve them.

A COA is the document a customer receives that says *this batch meets specification*,
and somebody's name is on it. If DIS issues one and it is wrong, that is not a bad
chart — it is a released batch that should not have been.

Three invariants, three groups of tests. The first is the one that matters most: an
incomplete certificate must never read as passing.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from coa import services
from coa.builder import CoaError, build
from coa.models import Certificate, Signature, content_hash
from coa.renderer import to_html
from coa.signing import SigningError, sign
from connections.models import DataSource

PASSWORD = "Refinery@123"


class _LimsBase(TestCase):
    """A LIMS-shaped SQLite source: SAMPLE (header), TEST (ordered), RESULT (reported)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "lims.sqlite")
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE sample (sample_number INTEGER, text_id TEXT, product TEXT,
              sample_type TEXT, status TEXT, in_spec TEXT, login_date TEXT,
              date_completed TEXT);
            CREATE TABLE result (sample_number INTEGER, result_number INTEGER,
              analysis TEXT, name TEXT, numeric_entry TEXT, units TEXT, in_spec TEXT,
              entered_by TEXT, entered_on TEXT);
            CREATE TABLE test (sample_number INTEGER, test_number INTEGER,
              analysis TEXT, status TEXT, instrument TEXT);

            -- 1: fully reported, all in spec  -> CONFORMS
            INSERT INTO sample VALUES (1,'S-0001','Diesel','SCHEDULED','C','T',
              '2026-07-01 08:00','2026-07-02 10:00');
            INSERT INTO test   VALUES (1,1,'SULFUR_TOTAL','C',''), (1,2,'CETANE_INDEX','C','');
            INSERT INTO result VALUES
              (1,1,'SULFUR_TOTAL','Sulfur','8.4','ppm','T','a.hassan','2026-07-02 09:00'),
              (1,2,'CETANE_INDEX','Cetane','51.2','','T','a.hassan','2026-07-02 09:30');

            -- 2: one result off-spec -> DOES NOT CONFORM
            INSERT INTO sample VALUES (2,'S-0002','Diesel','SCHEDULED','C','F',
              '2026-07-01 08:00','2026-07-02 10:00');
            INSERT INTO test   VALUES (2,1,'SULFUR_TOTAL','C','');
            INSERT INTO result VALUES
              (2,1,'SULFUR_TOTAL','Sulfur','92.0','ppm','F','m.said','2026-07-02 09:00');

            -- 3: THE DANGEROUS ONE. Two tests ordered, only one reported, and the
            --    reported one passed. A naive builder prints PASS.
            INSERT INTO sample VALUES (3,'S-0003','Gasoline','SCHEDULED','I','T',
              '2026-07-03 08:00',NULL);
            INSERT INTO test   VALUES (3,1,'OCTANE_NO','C',''), (3,2,'BENZENE','I','');
            INSERT INTO result VALUES
              (3,1,'OCTANE_NO','Octane','95.4','','T','n.farouk','2026-07-03 12:00');

            -- 4: a result with NO in-spec decision recorded
            INSERT INTO sample VALUES (4,'S-0004','Kerosene','SCHEDULED','C','',
              '2026-07-03 08:00','2026-07-04 08:00');
            INSERT INTO test   VALUES (4,1,'FLASH_POINT','C','');
            INSERT INTO result VALUES
              (4,1,'FLASH_POINT','Flash','38.0','C','','s.ibrahim','2026-07-04 07:00');

            -- 5: a cancelled test needs no result and must NOT block the certificate
            INSERT INTO sample VALUES (5,'S-0005','Diesel','SCHEDULED','C','T',
              '2026-07-03 08:00','2026-07-04 08:00');
            INSERT INTO test   VALUES (5,1,'SULFUR_TOTAL','C',''), (5,2,'CETANE_INDEX','X','');
            INSERT INTO result VALUES
              (5,1,'SULFUR_TOTAL','Sulfur','8.0','ppm','T','a.hassan','2026-07-04 07:00');
        """)
        con.commit(); con.close()

        User = get_user_model()
        self.user = User.objects.create_user(username="qa.manager", password=PASSWORD,
                                             role="qa_manager", first_name="Amira",
                                             last_name="Hassan")
        self.ds = DataSource.objects.create(
            name="EGPC_LIMS", source_type="sqlite", database_name=self.db,
            options={"path": self.db}, owner=self.user)

    def tearDown(self):
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# 1. An incomplete certificate never reads as passing.
# ---------------------------------------------------------------------------
class IncompleteNeverPassesTests(_LimsBase):
    def test_a_fully_reported_in_spec_sample_conforms(self):
        c = build(self.ds, 1)
        self.assertEqual(c["verdict"], "conforms")
        self.assertEqual(c["tests_ordered"], 2)
        self.assertEqual(c["tests_reported"], 2)
        self.assertEqual(c["missing_tests"], [])

    def test_an_off_spec_result_does_not_conform(self):
        c = build(self.ds, 2)
        self.assertEqual(c["verdict"], "does_not_conform")
        self.assertIn("SULFUR_TOTAL", c["verdict_reason"])

    def test_a_test_ordered_but_never_reported_makes_it_INCOMPLETE(self):
        """The whole reason this module exists.

        Sample 3 ordered two tests, reported one, and the one it reported PASSED. Every
        result present is in spec. A builder that only looks at results prints
        CONFORMS — and a batch goes out the door having never been tested for benzene.
        """
        c = build(self.ds, 3)

        self.assertEqual(c["verdict"], "incomplete")
        self.assertNotEqual(c["verdict"], "conforms")
        # And it names the test, on the face of the document.
        self.assertEqual([m["analysis"] for m in c["missing_tests"]], ["BENZENE"])
        self.assertIn("BENZENE", c["verdict_reason"])
        self.assertIn("cannot be certified as conforming", c["verdict_reason"])

    def test_a_result_with_no_in_spec_decision_is_not_a_pass(self):
        """An empty IN_SPEC flag means nobody adjudicated it. That is not a pass."""
        c = build(self.ds, 4)
        self.assertEqual(c["verdict"], "incomplete")
        self.assertEqual(c["unadjudicated"], ["FLASH_POINT"])
        self.assertIsNone(c["results"][0]["conforms"])

    def test_a_cancelled_test_does_not_block_the_certificate(self):
        """A cancelled test was never going to produce a result. Holding the
        certificate open forever would be its own kind of wrong — but the customer is
        still told it was cancelled."""
        c = build(self.ds, 5)
        self.assertEqual(c["verdict"], "conforms")
        self.assertEqual(c["missing_tests"], [])
        self.assertEqual([t["analysis"] for t in c["cancelled_tests"]], ["CETANE_INDEX"])

    def test_a_failure_beats_incompleteness(self):
        """Already failed AND incomplete is still a failure — incompleteness cannot
        rescue a sample that has already breached spec."""
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO test VALUES (2,2,'CETANE_INDEX','I','')")
        con.commit(); con.close()

        c = build(self.ds, 2)
        self.assertEqual(c["verdict"], "does_not_conform")
        self.assertTrue(c["missing_tests"])          # it IS also incomplete…
        self.assertEqual(c["verdict"], "does_not_conform")   # …and still a failure

    def test_an_unknown_sample_is_refused(self):
        with self.assertRaises(CoaError):
            build(self.ds, 9999)


# ---------------------------------------------------------------------------
# 2. A signature is bound to the bytes it signed (21 CFR Part 11 §11.70).
# ---------------------------------------------------------------------------
class SignatureBindingTests(_LimsBase):
    def _cert(self, sample=1):
        return services.issue(self.ds, sample, user=self.user)

    def test_signing_requires_the_password(self):
        """Being logged in is not signing. An unattended terminal must not be able to
        release a batch."""
        cert = self._cert()
        with self.assertRaises(SigningError) as ctx:
            sign(cert, user=self.user, password="wrong", meaning="approved")
        self.assertIn("Password does not match", str(ctx.exception))
        self.assertEqual(cert.signatures.count(), 0)

    def test_an_approved_certificate_carries_a_manifestation(self):
        cert = self._cert()
        s = sign(cert, user=self.user, password=PASSWORD, meaning="approved",
                 reason="Reviewed against ASTM D975.")

        cert.refresh_from_db()
        self.assertEqual(cert.status, Certificate.Status.APPROVED)
        self.assertTrue(cert.is_approved)
        # §11.50: printed name, date/time, and MEANING.
        self.assertEqual(s.printed_name, "Amira Hassan")
        self.assertEqual(s.meaning, Signature.Meaning.APPROVED)
        self.assertIsNotNone(s.signed_at)
        self.assertTrue(s.valid)

    def test_tampering_with_the_content_INVALIDATES_the_signature(self):
        """§11.70. The signature is bound to what it signed, not to which row it sits
        beside. Change the certificate and the signature must visibly break.

        The attack modelled here is the one that matters: take a signed certificate
        that says DOES NOT CONFORM and quietly flip the failing result to a pass.
        """
        cert = self._cert(sample=2)                  # SULFUR_TOTAL is off-spec
        self.assertEqual(cert.verdict, Certificate.Verdict.DOES_NOT_CONFORM)

        sign(cert, user=self.user, password=PASSWORD, meaning="approved",
             reason="Certifying the failure.")
        self.assertTrue(cert.valid_signatures)
        original_hash = cert.content_hash

        # Somebody edits the failing result to make it look like a pass.
        tampered = dict(cert.content)
        tampered["results"] = [dict(r, conforms=True, in_spec="T")
                               for r in tampered["results"]]
        tampered["verdict"] = "conforms"
        cert.content = tampered
        cert.save()

        cert.refresh_from_db()
        self.assertNotEqual(cert.content_hash, original_hash)
        self.assertEqual(cert.valid_signatures, [])
        self.assertTrue(cert.broken_signatures)
        self.assertFalse(cert.is_approved)
        # And the document says so, in terms nobody can miss.
        self.assertIn("TAMPERED", to_html(cert))

    def test_an_INCOMPLETE_certificate_cannot_be_approved(self):
        """Authority does not substitute for a missing result. Refuse at the door."""
        cert = services.issue(self.ds, 3, user=self.user)   # BENZENE never reported
        self.assertEqual(cert.verdict, Certificate.Verdict.INCOMPLETE)

        with self.assertRaises(SigningError) as ctx:
            sign(cert, user=self.user, password=PASSWORD, meaning="approved")
        self.assertIn("INCOMPLETE and cannot be approved", str(ctx.exception))

        cert.refresh_from_db()
        self.assertEqual(cert.status, Certificate.Status.DRAFT)
        self.assertEqual(cert.signatures.count(), 0)

    def test_an_incomplete_certificate_CAN_be_rejected(self):
        cert = services.issue(self.ds, 3, user=self.user)
        sign(cert, user=self.user, password=PASSWORD, meaning="rejected",
             reason="Benzene not reported.")
        cert.refresh_from_db()
        self.assertEqual(cert.status, Certificate.Status.REJECTED)

    def test_the_content_hash_is_derived_never_supplied(self):
        """Otherwise a caller could hand us a hash that matches a signature while the
        content says something else entirely."""
        cert = self._cert()
        cert.content_hash = "0" * 64
        cert.save()
        cert.refresh_from_db()
        self.assertEqual(cert.content_hash, content_hash(cert.content))


# ---------------------------------------------------------------------------
# 3. Issuing, superseding, and what the document actually says.
# ---------------------------------------------------------------------------
class IssuanceTests(_LimsBase):
    def test_re_issuing_supersedes_rather_than_edits(self):
        """A document a customer already holds does not get edited underneath them."""
        first = services.issue(self.ds, 1, user=self.user)
        second = services.issue(self.ds, 1, user=self.user)

        first.refresh_from_db()
        self.assertEqual(first.status, Certificate.Status.SUPERSEDED)
        self.assertEqual(first.superseded_by, second)
        self.assertEqual(first.number, "COA-1-01")
        self.assertEqual(second.number, "COA-1-02")   # the revision is in the number

    def test_issuing_is_audited_with_the_verdict_and_the_hash(self):
        from core.models import AuditLog

        cert = services.issue(self.ds, 3, user=self.user)
        entry = AuditLog.objects.get(target_type="coa.Certificate")
        self.assertEqual(entry.detail["verdict"], "incomplete")
        self.assertEqual(entry.detail["missing_tests"], ["BENZENE"])
        self.assertEqual(entry.detail["content_hash"], cert.content_hash)

    def test_a_draft_is_stamped_and_says_it_is_not_valid_for_release(self):
        cert = services.issue(self.ds, 1, user=self.user)
        html = to_html(cert)
        self.assertIn("DRAFT", html)
        self.assertIn("not valid for release", html)

    def test_an_approved_certificate_is_not_stamped_draft(self):
        cert = services.issue(self.ds, 1, user=self.user)
        sign(cert, user=self.user, password=PASSWORD, meaning="approved")
        cert.refresh_from_db()
        html = to_html(cert)
        self.assertNotIn('class="draft"', html)
        self.assertIn("Approved by: Amira Hassan", html)

    def test_the_document_prints_the_missing_test_rather_than_leaving_a_gap(self):
        """A gap in a table is invisible. A row that says NOT REPORTED is not."""
        cert = services.issue(self.ds, 3, user=self.user)
        html = to_html(cert)
        self.assertIn("INCOMPLETE — CANNOT CERTIFY", html)
        self.assertIn("Ordered — NOT REPORTED", html)
        self.assertIn("BENZENE", html)

    def test_the_document_states_that_no_figure_came_from_a_language_model(self):
        cert = services.issue(self.ds, 1, user=self.user)
        self.assertIn("No figure is produced by a language model", to_html(cert))


class CoaApiTests(APITestCase, _LimsBase):
    def setUp(self):
        _LimsBase.setUp(self)
        self.client.force_authenticate(self.user)

    def test_issue_sign_and_render(self):
        r = self.client.post("/api/coa/", {"datasource": self.ds.id,
                                           "sample_number": 1}, format="json")
        self.assertEqual(r.status_code, 201)
        cert_id = r.json()["id"]
        self.assertEqual(r.json()["verdict"], "conforms")
        self.assertEqual(r.json()["status"], "draft")

        bad = self.client.post(f"/api/coa/{cert_id}/sign/",
                               {"password": "nope", "meaning": "approved"}, format="json")
        self.assertEqual(bad.status_code, 400)

        ok = self.client.post(f"/api/coa/{cert_id}/sign/",
                              {"password": PASSWORD, "meaning": "approved"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["status"], "approved")
        self.assertEqual(ok.json()["signatures"], 1)

        html = self.client.get(f"/api/coa/{cert_id}/render/")
        self.assertEqual(html.status_code, 200)
        self.assertIn(b"Approved by", html.content)

    def test_an_incomplete_certificate_is_refused_approval_over_the_wire(self):
        r = self.client.post("/api/coa/", {"datasource": self.ds.id,
                                           "sample_number": 3}, format="json")
        cert_id = r.json()["id"]
        self.assertEqual(r.json()["verdict"], "incomplete")
        self.assertFalse(r.json()["certifiable"])
        self.assertEqual(r.json()["missing_tests"], ["BENZENE"])

        sig = self.client.post(f"/api/coa/{cert_id}/sign/",
                               {"password": PASSWORD, "meaning": "approved"},
                               format="json")
        self.assertEqual(sig.status_code, 400)
        self.assertIn("INCOMPLETE", sig.json()["detail"])

    def test_a_refused_signature_is_itself_audited(self):
        """Repeated failed signings on a release-critical document are exactly what
        Part 11 §11.300(d) asks you to be able to detect."""
        from core.models import AuditLog

        r = self.client.post("/api/coa/", {"datasource": self.ds.id,
                                           "sample_number": 1}, format="json")
        self.client.post(f"/api/coa/{r.json()['id']}/sign/",
                         {"password": "wrong", "meaning": "approved"}, format="json")

        self.assertTrue(AuditLog.objects.filter(
            summary__startswith="Signature REFUSED").exists())

    def test_rendering_a_certificate_is_audited_as_a_record_leaving_the_building(self):
        from core.models import AuditLog

        r = self.client.post("/api/coa/", {"datasource": self.ds.id,
                                           "sample_number": 1}, format="json")
        self.client.get(f"/api/coa/{r.json()['id']}/render/?fmt=html")
        self.assertTrue(AuditLog.objects.filter(summary__startswith="Rendered").exists())
