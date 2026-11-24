"""The runtime security posture check — the Installation Qualification, made executable.

Six requirements were marked OPERATOR TO VERIFY on the grounds that no unit test can
prove them. That is true of a unit test, and it was a little lazy: a command that runs
*on the installed system* can prove most of them, and a control nobody runs is a control
nobody has.

The two properties that matter, and are tested here:

* **It never writes.** Proving a read-only grant by attempting an INSERT into a GxP LIMS
  is a cure worse than the disease. Both SQL Server and Postgres answer the question
  from metadata, so we ask.
* **It does not soften.** UNKNOWN is not a pass, a superuser login is not "a missing
  DENY", and a check that could not run has proven nothing.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from connections.models import DataSource
from validation import preflight
from validation.preflight import FAIL, PASS, UNKNOWN, WARN


class TlsChecksTests(SimpleTestCase):
    @override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False,
                       CSRF_COOKIE_SECURE=False, CORS_ALLOWED_ORIGINS=[],
                       CSRF_TRUSTED_ORIGINS=[])
    def test_plaintext_is_a_failure_not_a_warning(self):
        c = preflight.check_tls()[0]
        self.assertEqual(c.status, FAIL)
        self.assertIn("cleartext", c.detail)
        self.assertIn("make tls", c.fix)

    @override_settings(SECURE_SSL_REDIRECT=True, SESSION_COOKIE_SECURE=True,
                       CSRF_COOKIE_SECURE=True, SECURE_HSTS_SECONDS=31536000,
                       CORS_ALLOWED_ORIGINS=["https://dis.example.com"],
                       CSRF_TRUSTED_ORIGINS=["https://dis.example.com"])
    def test_a_properly_configured_edge_passes(self):
        for c in preflight.check_tls():
            self.assertEqual(c.status, PASS, c.detail)

    @override_settings(SECURE_SSL_REDIRECT=True, SESSION_COOKIE_SECURE=True,
                       CSRF_COOKIE_SECURE=True,
                       CORS_ALLOWED_ORIGINS=["http://89.35.193.15"],
                       CSRF_TRUSTED_ORIGINS=[])
    def test_an_https_app_that_still_trusts_http_origins_is_not_enforcing_anything(self):
        origins = [c for c in preflight.check_tls() if "origins" in c.name][0]
        self.assertEqual(origins.status, FAIL)
        self.assertIn("89.35.193.15", origins.detail)


class LimsGrantTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ops", password="x")

    def _ds(self, username, name="LIMS"):
        return DataSource.objects.create(
            name=name, source_type="mssql", host="10.0.0.1", database_name="EGPC",
            schema_name="dbo", username=username, owner=self.user)

    def test_connecting_as_the_database_superuser_is_reported_WITHOUT_touching_the_LIMS(self):
        """Naming the account is enough. If DIS holds sysadmin on the laboratory's
        system of record, the grant question is moot and probing it would be beside the
        point — and we must not touch a GxP database to learn something we already know.
        """
        self._ds("sa")
        with mock.patch("validation.preflight._probe_write_grants") as probe:
            checks = preflight.check_lims_readonly()

        probe.assert_not_called()          # it never even connected
        c = checks[0]
        self.assertEqual(c.status, FAIL)
        self.assertIn("SUPERUSER", c.detail)
        self.assertIn("There is no backstop", c.detail)
        self.assertIn("Rotate", c.fix)     # the password has been sitting in a config file
        self.assertTrue(c.evidence["superuser"])

    def test_a_writable_ordinary_account_fails(self):
        self._ds("dis_reader")
        with mock.patch("validation.preflight._probe_write_grants",
                        return_value={"ins": True, "upd": False, "del": False}):
            c = preflight.check_lims_readonly()[0]
        self.assertEqual(c.status, FAIL)
        self.assertIn("CAN INS", c.detail)

    def test_a_properly_granted_account_passes(self):
        self._ds("dis_readonly")
        with mock.patch("validation.preflight._probe_write_grants",
                        return_value={"ins": False, "upd": False, "del": False}):
            c = preflight.check_lims_readonly()[0]
        self.assertEqual(c.status, PASS)

    def test_an_unreachable_source_is_UNKNOWN_not_a_pass(self):
        """A check that could not run has proven nothing. Reporting it green because it
        did not report red is how a system gets certified without being safe."""
        self._ds("dis_readonly")
        with mock.patch("validation.preflight._probe_write_grants",
                        side_effect=RuntimeError("login timeout")):
            c = preflight.check_lims_readonly()[0]
        self.assertEqual(c.status, UNKNOWN)
        self.assertNotEqual(c.status, PASS)

    def test_the_probe_is_metadata_only(self):
        """It must never attempt a write — not even one it would roll back. Both engines
        answer 'may I?' from the catalogue."""
        for sql in preflight._WRITE_PROBE.values():
            upper = sql.upper()
            for verb in ("INSERT INTO", "UPDATE ", "DELETE ", "MERGE", "BEGIN", "COMMIT"):
                self.assertNotIn(verb, upper, sql)
            self.assertTrue(upper.lstrip().startswith("SELECT"))


class AppendOnlyAtTheDatabaseTests(SimpleTestCase):
    """The audit trail is the one record that proves what everyone did, and it was the one
    record any of them could rewrite: append-only was enforced in the application, and the
    application is not what an attacker with the database password is going through.

    The interesting part is what running the fix against a real Postgres taught me. The old
    check asked `has_table_privilege(…, 'UPDATE')` and prescribed a REVOKE. Both are useless
    here, because **the app connects as a superuser** — superusers bypass privilege checks,
    so the check would report FAIL forever and its remediation would never work. A trigger,
    by contrast, refuses the statement whatever the privileges say. These tests pin that.
    """

    SCRIPT = Path(__file__).resolve().parent.parent / "db" / "protect-audit-trail-postgres.sql"

    def test_a_non_postgres_database_is_UNKNOWN_not_a_pass(self):
        with mock.patch("django.db.connection.vendor", "sqlite"):
            c = preflight.check_audit_grants()[0]
        self.assertEqual(c.status, UNKNOWN)
        self.assertNotEqual(c.status, PASS)

    def test_the_fix_no_longer_prescribes_a_bare_REVOKE(self):
        """The regression guard for the bug this phase found. A REVOKE against a superuser
        does nothing, and an IQ check whose remediation cannot remediate is worse than no
        check: it sends someone to do a job that cannot succeed, then tells them they
        failed."""
        source = Path(preflight.__file__).read_text()
        # The word may appear in explanation; what must not appear is a REVOKE presented as
        # THE fix, with nothing else.
        self.assertNotIn('fix="REVOKE UPDATE, DELETE ON core_auditlog FROM <app_role>;"',
                         source)
        self.assertIn("protect-audit-trail-postgres.sql", source)

    def test_the_script_protects_BOTH_tables(self):
        """A signature that can be edited after the fact is not a signature."""
        sql = self.SCRIPT.read_text()
        for table in ("core_auditlog", "coa_signature"):
            self.assertIn(f"BEFORE UPDATE OR DELETE ON {table}", sql)

    def test_the_script_uses_a_trigger_and_not_only_a_grant(self):
        """The grant is what starts working the day the app stops being a superuser. The
        trigger is what holds the line today."""
        sql = self.SCRIPT.read_text().upper()
        self.assertIn("CREATE TRIGGER", sql)
        self.assertIn("REVOKE UPDATE, DELETE", sql)

    def test_the_script_never_touches_INSERT(self):
        """Append-only means the trail can still be WRITTEN. A protection that also stopped
        the inserts would take the audit trail down with it — and audit writes fail closed,
        so it would take the whole application down too."""
        sql = self.SCRIPT.read_text().upper()
        self.assertNotIn("REVOKE INSERT", sql)
        self.assertNotIn("BEFORE INSERT", sql)

    def test_the_script_says_out_loud_what_it_cannot_do(self):
        """No database defends against its own superuser. A control that oversells itself is
        how a system gets certified without being safe."""
        sql = self.SCRIPT.read_text().lower()
        self.assertIn("superuser", sql)
        self.assertIn("disable trigger", sql)


class TheModelAnsweringIsTheModelQualifiedTests(SimpleTestCase):
    """`LLM_MODEL=qwen2.5:7b-instruct` is a mutable tag, and Ollama has no digest-pinned
    reference — so the old advice ("pin the model by digest") was something nobody could do.

    The answerable question is whether it has MOVED. If someone re-pulls the tag, the weights
    change silently, and the OQ, the golden-set results and the whole /validation page carry
    on describing a model that is no longer the one answering. That is not stale evidence. It
    is void evidence, and it must fail.
    """

    @override_settings(LLM_MODEL="qwen2.5:7b", DSE_LLM_MODEL_DIGEST="")
    def test_no_recorded_digest_is_a_WARNING_not_a_silence(self):
        c = [c for c in preflight.check_posture()
             if "model that was qualified" in c.name][0]
        self.assertEqual(c.status, WARN)
        self.assertIn("make pin", c.fix)

    @override_settings(LLM_MODEL="qwen2.5:7b", DSE_LLM_MODEL_DIGEST="sha256:aaa")
    def test_an_unchanged_model_passes(self):
        with mock.patch("validation.model_identity.live_digest",
                        return_value=("sha256:aaa", "")):
            c = [c for c in preflight.check_posture()
                 if "model that was qualified" in c.name][0]
        self.assertEqual(c.status, PASS)

    @override_settings(LLM_MODEL="qwen2.5:7b", DSE_LLM_MODEL_DIGEST="sha256:aaa")
    def test_a_CHANGED_model_is_a_FAILURE_and_says_the_qualification_is_void(self):
        with mock.patch("validation.model_identity.live_digest",
                        return_value=("sha256:bbb", "")):
            c = [c for c in preflight.check_posture()
                 if "model that was qualified" in c.name][0]
        self.assertEqual(c.status, FAIL)
        self.assertIn("void", c.detail)
        self.assertEqual(c.evidence, {"recorded": "sha256:aaa", "running": "sha256:bbb"})

    @override_settings(LLM_MODEL="qwen2.5:7b", DSE_LLM_MODEL_DIGEST="sha256:aaa")
    def test_an_unreachable_model_host_is_UNKNOWN_and_NOT_agreement(self):
        """The failure that would quietly undo the whole check: treating "I could not ask"
        as "it is fine"."""
        with mock.patch("validation.model_identity.live_digest",
                        return_value=(None, "connection refused")):
            c = [c for c in preflight.check_posture()
                 if "model that was qualified" in c.name][0]
        self.assertEqual(c.status, UNKNOWN)
        self.assertNotEqual(c.status, PASS)
        self.assertIn("not knowing", c.detail)

    def test_the_fix_no_longer_tells_people_to_do_the_impossible(self):
        """Ollama has no `model@sha256:…` reference. Advice that cannot be followed is the
        same failure as the REVOKE a superuser ignores: it sends someone to do a job that
        cannot succeed, then reports that they failed."""
        source = Path(preflight.__file__).read_text()
        self.assertNotIn('fix="Pin the model by digest and record it."', source)


class BackupTests(SimpleTestCase):
    """The check that must never go green from in here.

    Preflight runs inside the api container; the backups are host files it cannot see.
    The tempting "fix" is to have backup-db.sh export a variable and let preflight read
    it — at which point the control is reporting on itself, and URS-064 is satisfied by
    a backup nobody has ever restored.
    """

    def test_it_is_UNKNOWN_and_can_never_be_a_pass(self):
        c = preflight.check_backups()[0]
        self.assertEqual(c.status, UNKNOWN)
        self.assertNotEqual(c.status, PASS)

    def test_it_stays_UNKNOWN_no_matter_what_the_environment_claims(self):
        """If someone wires an env var into this check, this test is what stops them."""
        with mock.patch.dict(os.environ, {
            "BACKUP_VERIFIED": "1", "BACKUP_PASSPHRASE": "x",
            "BACKUP_REMOTE": "s3:prod", "DIS_BACKUP_OK": "true",
        }):
            self.assertEqual(preflight.check_backups()[0].status, UNKNOWN)

    def test_it_names_the_command_that_can_actually_prove_it(self):
        """UNKNOWN is only honest if it also says how to make it known. A gap with no
        route out of it is just a shrug."""
        c = preflight.check_backups()[0]
        self.assertIn("verify-backup", c.fix)
        self.assertEqual(c.id, "URS-064")


class PostureTests(SimpleTestCase):
    @override_settings(DEBUG=True)
    def test_debug_on_is_a_failure(self):
        c = [c for c in preflight.check_posture() if c.name == "DEBUG is off"][0]
        self.assertEqual(c.status, FAIL)

    @override_settings(AUDIT_FAIL_OPEN=True)
    def test_audit_fail_open_is_a_failure(self):
        c = [c for c in preflight.check_posture()
             if c.name == "Audit writes fail closed"][0]
        self.assertEqual(c.status, FAIL)
        self.assertIn("voids the attributability claim", c.detail)

    @override_settings(DSE_ALLOW_EXTERNAL_LLM=True)
    def test_egress_enabled_is_a_warning_that_names_the_consent_needed(self):
        """Egress is a legitimate configuration — but prompts carry sample rows, so it
        is a decision somebody must have made on purpose and in writing."""
        c = [c for c in preflight.check_posture() if c.name == "No LLM egress"][0]
        self.assertEqual(c.status, WARN)
        self.assertIn("agreed to it in writing", c.detail)


class KnownGapsAreNotForgottenTests(TestCase):
    """This class used to assert that the plaintext-token gap was *reported*. It is now
    fixed, so it asserts the fix — and, more to the point, that it cannot come undone."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="t", password="x")

    def test_the_plaintext_column_is_gone_and_preflight_confirms_it(self):
        from accounts.models import APIToken

        APIToken.generate(self.user, "ci")
        c = [c for c in preflight.check_known_gaps()
             if c.name == "API tokens are hashed"][0]
        self.assertEqual(c.status, PASS)
        self.assertIn("SHA-256", c.detail)

    def test_reintroducing_a_plaintext_key_column_would_FAIL_the_check(self):
        """The check is structural — it reads the model, not a changelog. If anyone ever
        adds `key` back, this is what turns the IQ red."""
        from accounts.models import APIToken

        real = APIToken._meta.get_fields
        fake = type("F", (), {"name": "key"})()
        with mock.patch.object(APIToken._meta, "get_fields",
                               side_effect=lambda *a, **k: list(real()) + [fake]):
            c = preflight.check_known_gaps()[0]
        self.assertEqual(c.status, FAIL)
        self.assertIn("plaintext", c.detail.lower())

    def test_a_never_expiring_token_is_still_flagged(self):
        """Fixed is not the same as finished: a credential with no end date outlives the
        person, the project and the reason it was issued."""
        from accounts.models import APIToken

        APIToken.generate(self.user, "forever")   # no expires_at
        warns = [c for c in preflight.check_known_gaps() if c.status == WARN]
        self.assertTrue(any("never expire" in c.detail for c in warns))


class RunnerTests(TestCase):
    def test_one_broken_probe_does_not_hide_the_others(self):
        with mock.patch("validation.preflight.check_fernet",
                        side_effect=RuntimeError("boom")):
            checks = preflight.run()
        self.assertTrue(any(c.status == UNKNOWN and "boom" in c.detail for c in checks))
        self.assertTrue(any(c.id == "URS-060" for c in checks))
