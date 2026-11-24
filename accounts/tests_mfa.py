"""Two-factor authentication — and the four ways it gets built wrong.

Part 11 §11.200(a)(1) wants two distinct identification components, and DIS already has them
(an identification code and a password, re-entered at signing). So MFA is not what makes us
compliant. It is what makes the *password* worth anything after it has been phished, reused,
or read out of somebody else's breach dump.

The tests that matter are not "does the code verify". They are:

* the login must not hand back a **session** when it still owes a second factor;
* a code must not be **replayable** inside its 30-second life;
* the secret must not be **readable** out of the database;
* and a role that is *obliged* to have MFA must not be able to decline it from the client.

Plus the one that makes all of it worth trusting: the TOTP is checked against **RFC 6238's own
published test vectors**, not against itself.
"""
from __future__ import annotations

import base64
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts import totp
from accounts.models import MfaDevice
from core.models import AuditLog

User = get_user_model()


class RfcTestVectorTests(APITestCase):
    """RFC 6238 Appendix B. Correctness the standard itself vouches for, rather than
    correctness we assert by having imported a package."""

    SEED = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    VECTORS = [
        (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
        (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
    ]

    def test_every_official_vector(self):
        for when, expected in self.VECTORS:
            self.assertEqual(totp.generate(self.SEED, when=when, digits=8), expected,
                             f"RFC 6238 vector at t={when}")

    def test_the_tolerance_window_is_ONE_step_as_the_rfc_recommends(self):
        """Every extra step is another 30 seconds in which an intercepted code still works.
        Widening this is not a kindness to users; it is a gift to whoever is watching them
        type."""
        self.assertEqual(totp.WINDOW, 1)

    def test_a_code_from_the_previous_step_is_still_accepted(self):
        now = 1_700_000_000
        prev = totp.generate(self.SEED, when=now - totp.STEP_SECONDS)
        self.assertIsNotNone(totp.verify(self.SEED, prev, when=now))

    def test_a_code_from_three_steps_ago_is_not(self):
        now = 1_700_000_000
        old = totp.generate(self.SEED, when=now - 3 * totp.STEP_SECONDS)
        self.assertIsNone(totp.verify(self.SEED, old, when=now))

    def test_rubbish_is_rejected_without_raising(self):
        for junk in ("", "abcdef", "12345", "1234567", None, "12 34 56"):
            self.assertIsNone(totp.verify(self.SEED, junk))


class MfaBase(APITestCase):
    def setUp(self):
        cache.clear()          # the login/mfa throttles count in the cache
        self.user = User.objects.create_user("nadia", password="correct-horse",
                                             role=User.Role.ANALYST)

    def _enrol(self, user=None):
        user = user or self.user
        self.client.force_authenticate(user)
        secret = self.client.post("/api/auth/mfa/setup/", {}, format="json").json()["secret"]
        codes = self.client.post("/api/auth/mfa/confirm/",
                                 {"code": totp.generate(secret)}, format="json"
                                 ).json()["backup_codes"]
        self.client.force_authenticate(None)
        return secret, codes

    def _password_step(self, password="correct-horse", username="nadia"):
        return self.client.post("/api/auth/token/",
                                {"username": username, "password": password}, format="json")


class TheLoginMustNotHandBackASessionTests(MfaBase):
    """The single way this feature gets built wrong: the password step returns a JWT
    *and* asks for a code. Then the code is a formality you can decline."""

    def test_the_password_step_returns_NO_tokens_when_mfa_is_on(self):
        self._enrol()
        body = self._password_step().json()

        self.assertTrue(body["mfa_required"])
        self.assertNotIn("access", body)
        self.assertNotIn("refresh", body)

    def test_the_challenge_token_is_NOT_usable_as_a_session(self):
        self._enrol()
        challenge = self._password_step().json()["mfa_token"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {challenge}")
        self.assertIn(self.client.get("/api/auth/me/").status_code, (401, 403))

    def test_a_correct_code_completes_the_login(self):
        secret, _ = self._enrol()
        challenge = self._password_step().json()["mfa_token"]

        # The NEXT code, not the current one. Confirming enrolment consumed the current
        # step — and it had to: the code they proved the app with must not then be reusable
        # to log in, which is exactly a replay. (The window is +/-1 step, so a next-step
        # code is accepted now.)
        r = self.client.post(
            "/api/auth/mfa/verify/",
            {"mfa_token": challenge,
             "code": totp.generate(secret, when=time.time() + totp.STEP_SECONDS)},
            format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.json())

    def test_the_code_used_to_ENROL_cannot_then_be_used_to_LOG_IN(self):
        """The same replay, wearing a different hat — and the one a hand-test would miss,
        because by the time you have finished scanning the QR the window has usually
        rolled."""
        secret, _ = self._enrol()
        challenge = self._password_step().json()["mfa_token"]

        r = self.client.post("/api/auth/mfa/verify/",
                             {"mfa_token": challenge, "code": totp.generate(secret)},
                             format="json")
        self.assertEqual(r.status_code, 401)

    def test_a_wrong_code_does_not(self):
        self._enrol()
        challenge = self._password_step().json()["mfa_token"]
        r = self.client.post("/api/auth/mfa/verify/",
                             {"mfa_token": challenge, "code": "000000"}, format="json")
        self.assertEqual(r.status_code, 401)

    def test_a_forged_challenge_is_refused(self):
        self._enrol()
        r = self.client.post("/api/auth/mfa/verify/",
                             {"mfa_token": "not-a-real-token", "code": "123456"},
                             format="json")
        self.assertEqual(r.status_code, 401)

    def test_a_user_without_mfa_logs_in_exactly_as_before(self):
        body = self._password_step().json()
        self.assertIn("access", body)
        self.assertNotIn("mfa_required", body)

    @override_settings(DSE_LOGIN_MAX_FAILURES=3)
    def test_failed_CODES_feed_the_account_lockout(self):
        """A six-digit code is a million guesses. A second factor you can query a million
        times is not a second factor."""
        self._enrol()
        challenge = self._password_step().json()["mfa_token"]

        for _ in range(3):
            self.client.post("/api/auth/mfa/verify/",
                             {"mfa_token": challenge, "code": "000000"}, format="json")

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_locked)


class ACodeCannotBeReplayedTests(MfaBase):
    """A TOTP code is valid for its whole 30-second step. Anyone who sees it — over a
    shoulder, in a screenshot, in a proxy log — can use it again inside that window. This is
    the control that gets left out, because a demo works perfectly without it."""

    def test_the_same_code_cannot_be_used_twice(self):
        secret, _ = self._enrol()
        dev = MfaDevice.objects.get(user=self.user)
        code = totp.generate(secret, when=time.time() + totp.STEP_SECONDS)

        self.assertTrue(dev.verify(code))
        self.assertFalse(dev.verify(code), "the code was accepted a second time")

    def test_an_OLDER_code_cannot_be_used_after_a_newer_one(self):
        secret, _ = self._enrol()
        dev = MfaDevice.objects.get(user=self.user)
        now = time.time() + 10 * totp.STEP_SECONDS

        newer = totp.generate(secret, when=now)
        older = totp.generate(secret, when=now - totp.STEP_SECONDS)

        self.assertTrue(totp.verify(secret, older, when=now) is not None)  # still in window
        dev.last_step = totp.step_at(now)
        dev.save(update_fields=["last_step"])
        self.assertFalse(dev.verify(older), "a code older than one already used was accepted")
        self.assertFalse(dev.verify(newer), "the current step was replayed")


class TheSecretIsNotReadableFromTheDatabaseTests(MfaBase):
    def test_the_stored_secret_is_encrypted(self):
        secret, _ = self._enrol()
        dev = MfaDevice.objects.get(user=self.user)

        self.assertNotEqual(dev.secret_encrypted, secret)
        self.assertNotIn(secret, str(MfaDevice.objects.filter(pk=dev.pk).values().first()))
        self.assertEqual(dev.secret, secret)     # …and it round-trips

    def test_it_is_ENCRYPTED_and_not_hashed_and_that_is_deliberate(self):
        """It cannot be hashed: verifying a code means recomputing the HMAC, which needs the
        key back. So it is a credential in the same class as a data-source password, and it
        is stored the same way."""
        secret, _ = self._enrol()
        self.assertEqual(MfaDevice.objects.get(user=self.user).secret, secret)


class EnrolmentTests(MfaBase):
    def test_setup_alone_changes_NOTHING_about_the_login(self):
        """A secret that goes live the moment it is generated locks the user out if they
        close the tab before scanning it — and then the way back is an administrator, at
        whatever hour they find out."""
        self.client.force_authenticate(self.user)
        self.client.post("/api/auth/mfa/setup/", {}, format="json")
        self.client.force_authenticate(None)

        body = self._password_step().json()
        self.assertIn("access", body)
        self.assertNotIn("mfa_required", body)

    def test_a_wrong_confirmation_code_does_not_enable_it(self):
        self.client.force_authenticate(self.user)
        self.client.post("/api/auth/mfa/setup/", {}, format="json")
        r = self.client.post("/api/auth/mfa/confirm/", {"code": "000000"}, format="json")

        self.assertEqual(r.status_code, 400)
        self.assertFalse(MfaDevice.objects.get(user=self.user).is_active)

    def test_the_setup_response_carries_a_QR_and_the_secret_for_manual_entry(self):
        self.client.force_authenticate(self.user)
        body = self.client.post("/api/auth/mfa/setup/", {}, format="json").json()

        self.assertTrue(body["otpauth_uri"].startswith("otpauth://totp/"))
        self.assertIn("<svg", body["qr_svg"])
        self.assertIn(body["secret"], body["otpauth_uri"])

    def test_enabling_it_is_audited(self):
        self._enrol()
        self.assertTrue(AuditLog.objects.filter(
            target_type="accounts.MfaDevice", summary__contains="MFA enabled").exists())


class BackupCodeTests(MfaBase):
    """A second factor with no way past it is a way to lock a chemist out of their own
    laboratory's data at two in the morning, with their phone in a taxi."""

    def test_a_backup_code_completes_the_login(self):
        _, codes = self._enrol()
        challenge = self._password_step().json()["mfa_token"]

        r = self.client.post("/api/auth/mfa/verify/",
                             {"mfa_token": challenge, "code": codes[0]}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.json())

    def test_a_backup_code_works_ONCE(self):
        _, codes = self._enrol()
        challenge = self._password_step().json()["mfa_token"]
        self.client.post("/api/auth/mfa/verify/",
                         {"mfa_token": challenge, "code": codes[0]}, format="json")

        challenge2 = self._password_step().json()["mfa_token"]
        r = self.client.post("/api/auth/mfa/verify/",
                             {"mfa_token": challenge2, "code": codes[0]}, format="json")
        self.assertEqual(r.status_code, 401, "a used backup code worked again")

    def test_they_are_stored_hashed(self):
        _, codes = self._enrol()
        dev = MfaDevice.objects.get(user=self.user)
        for code in codes:
            self.assertNotIn(code, str(dev.backup_code_hashes))
        self.assertEqual(len(dev.backup_code_hashes), len(codes))

    def test_using_one_is_audited_loudly(self):
        """A run of backup codes means somebody has lost a phone — or is working around
        one."""
        _, codes = self._enrol()
        challenge = self._password_step().json()["mfa_token"]
        self.client.post("/api/auth/mfa/verify/",
                         {"mfa_token": challenge, "code": codes[0]}, format="json")

        self.assertTrue(AuditLog.objects.filter(summary__contains="BACKUP CODE").exists())


class TurningItOffCostsSomethingTests(MfaBase):
    def test_disabling_requires_the_password_again(self):
        """A second factor you can remove from an already-open session — a borrowed laptop,
        an unlocked screen — is not a second factor. It is a setting."""
        self._enrol()
        self.client.force_authenticate(self.user)

        r = self.client.post("/api/auth/mfa/disable/", {"password": "wrong"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(MfaDevice.objects.filter(user=self.user).exists())

    def test_with_the_password_it_comes_off_and_is_audited(self):
        self._enrol()
        self.client.force_authenticate(self.user)

        r = self.client.post("/api/auth/mfa/disable/",
                             {"password": "correct-horse"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(MfaDevice.objects.filter(user=self.user).exists())
        self.assertTrue(AuditLog.objects.filter(summary__contains="MFA DISABLED").exists())


@override_settings(DSE_REQUIRE_MFA_ROLES=["analyst"])
class APolicyNoViewCanDropTests(MfaBase):
    """The first version of this was a DRF permission class in DEFAULT_PERMISSION_CLASSES,
    and it was **decorative**: nearly every viewset here overrides `permission_classes` or
    `get_permissions()`, and an override REPLACES the defaults. `DatasetViewSet` returns
    `[IsAuthenticated()]` for list — so a required-MFA user sailed straight in, and the
    "global" policy protected exactly the views that had never thought about permissions.

    These tests go through the real request path with a real bearer token, because
    `force_authenticate` bypasses middleware — and testing through it is precisely what would
    have hidden the bug.
    """

    def _bearer(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_a_required_user_without_mfa_is_refused_the_application(self):
        self._bearer(self.user)
        r = self.client.get("/api/datasets/")
        self.assertEqual(r.status_code, 403)
        self.assertIn("two-factor", str(r.json()).lower())

    def test_it_holds_even_on_a_view_that_overrides_permission_classes(self):
        """The regression guard for the bug. DatasetViewSet.get_permissions() returns
        [IsAuthenticated()] for `list` — which is why a permission class could not do this
        job."""
        from datasets.views import DatasetViewSet

        self.assertTrue(hasattr(DatasetViewSet, "get_permissions"),
                        "the view this test is about no longer overrides permissions")
        self._bearer(self.user)
        self.assertEqual(self.client.get("/api/datasets/").status_code, 403)

    def test_but_the_way_to_COMPLY_stays_open(self):
        """Refusing the login instead would leave them outside the building with the
        instructions locked inside it."""
        self._bearer(self.user)
        self.assertEqual(self.client.post("/api/auth/mfa/setup/", {}, format="json")
                         .status_code, 200)
        self.assertEqual(self.client.get("/api/auth/mfa/").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)

    def test_once_enrolled_the_application_opens(self):
        self._enrol()
        self._bearer(self.user)
        self.assertEqual(self.client.get("/api/datasets/").status_code, 200)

    def test_a_role_that_is_NOT_required_is_unaffected(self):
        viewer = User.objects.create_user("viewer", password="x", role=User.Role.VIEWER)
        self._bearer(viewer)
        self.assertEqual(self.client.get("/api/datasets/").status_code, 200)

    def test_the_login_tells_them_they_must_enrol(self):
        body = self._password_step().json()
        self.assertTrue(body.get("mfa_enrolment_required"))
        self.assertIn("access", body)     # they are let IN — just not let anywhere
