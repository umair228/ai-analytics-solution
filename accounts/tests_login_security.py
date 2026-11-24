"""Detecting and reporting unauthorised use — 21 CFR Part 11 §11.300(d).

``LoginView`` recorded a login only ``if response.status_code == 200``. So a **failed**
login wrote nothing, anywhere: the audit trail could not tell a quiet night from a night
spent guessing passwords. There was no lockout and no rate limit, and
``POST /api/auth/register/`` would mint an account for anyone who asked.

That last one was survivable only by accident — a stranger's new account could see nothing,
because visibility was decided entirely by owner/shared_with and both were empty for them.
The moment ``Visibility.ORG`` began to mean something, an open registration endpoint became
"read every organisation-wide dataset in the laboratory", for anyone who could reach the
box, over the plaintext HTTP the pilot still serves.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts import lockout
from core.models import AuditLog

User = get_user_model()

class LoginTestCase(APITestCase):
    """The rate limiter counts attempts per address, in the cache, and that cache outlives
    a test. Every class here therefore starts from a clean counter — otherwise the tests
    interfere with each other through a shared bucket and start failing in whichever order
    they happen to run.

    Note there is no attempt to *disable* the throttle: DRF freezes `THROTTLE_RATES` as a
    class attribute at import, so `override_settings` cannot reach it, and an override that
    silently does nothing is worse than none. These tests run against the **shipped**
    rate — which is the thing worth testing anyway.
    """

    def setUp(self):
        cache.clear()


class FailedLoginsAreRecordedTests(LoginTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("nadia", password="correct-horse")

    def _login(self, password, username="nadia"):
        return self.client.post("/api/auth/token/",
                                {"username": username, "password": password},
                                format="json")

    def test_a_failed_login_is_written_to_the_audit_trail(self):
        """It produced nothing at all before."""
        self.assertEqual(self._login("wrong").status_code, 401)

        entry = AuditLog.objects.get(action=AuditLog.Action.LOGIN_FAILED)
        self.assertEqual(entry.user, self.user)
        self.assertIn("nadia", entry.summary)

    def test_a_failure_against_a_username_that_does_not_exist_is_recorded_too(self):
        """A burst of failures against names that are not accounts is what username
        enumeration looks like. A trail that only records attempts on *real* accounts
        cannot show it."""
        self._login("whatever", username="administrator")

        entry = AuditLog.objects.get(action=AuditLog.Action.LOGIN_FAILED)
        self.assertIsNone(entry.user)
        self.assertFalse(entry.detail["account_exists"])
        self.assertEqual(entry.detail["username"], "administrator")

    def test_a_successful_login_is_still_recorded(self):
        self.assertEqual(self._login("correct-horse").status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOGIN).exists())


@override_settings(DSE_LOGIN_MAX_FAILURES=3, DSE_LOGIN_LOCKOUT_MINUTES=15)
class LockoutTests(LoginTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("nadia", password="correct-horse",
                                             email="nadia@lab.local")
        User.objects.create_user("sec", password="x", role=User.Role.ADMIN,
                                 email="security@lab.local")

    def _login(self, password):
        return self.client.post("/api/auth/token/",
                                {"username": "nadia", "password": password},
                                format="json")

    def test_the_account_locks_after_the_threshold(self):
        for _ in range(3):
            self._login("wrong")

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_locked)
        self.assertEqual(self.user.failed_login_count, 3)

    def test_a_locked_account_is_refused_even_with_the_RIGHT_password(self):
        for _ in range(3):
            self._login("wrong")
        self.assertEqual(self._login("correct-horse").status_code, 401)

    def test_the_refusal_does_not_reveal_that_the_account_is_locked(self):
        """Saying "that account is locked" confirms the username exists AND hands over a
        denial-of-service in one sentence. Every failure looks the same."""
        for _ in range(3):
            self._login("wrong")
        body = str(self._login("correct-horse").json()).lower()
        for word in ("lock", "locked", "attempts", "too many"):
            self.assertNotIn(word, body)

    def test_the_lock_EXPIRES_rather_than_needing_an_administrator_at_3am(self):
        for _ in range(3):
            self._login("wrong")
        self.user.refresh_from_db()

        self.user.locked_until = timezone.now() - timezone.timedelta(seconds=1)
        self.user.save(update_fields=["locked_until"])
        self.assertFalse(self.user.is_locked)
        self.assertEqual(self._login("correct-horse").status_code, 200)

    def test_a_good_login_resets_the_counter(self):
        """The threshold is *consecutive* failures. Without this, five typos spread over a
        year would lock the account, and the lockout would be measuring forgetfulness."""
        self._login("wrong")
        self._login("wrong")
        self.assertEqual(self._login("correct-horse").status_code, 200)

        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_count, 0)

    def test_the_lockout_is_reported_urgently_not_merely_recorded(self):
        """§11.300(d) asks for reporting "in an immediate and urgent manner". A row in a
        table nobody opens is not a report."""
        for _ in range(3):
            self._login("wrong")

        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOCKOUT).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("locked out", mail.outbox[0].subject)
        self.assertIn("security@lab.local", mail.outbox[0].to)

    def test_a_locked_account_is_NOT_the_same_as_a_deactivated_one(self):
        """Conflating them means an attacker can make a leaver look present, or an
        offboarding look like an attack."""
        for _ in range(3):
            self._login("wrong")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_locked)
        self.assertTrue(self.user.is_active)

    def test_an_administrator_can_unlock_and_the_unlock_is_itself_audited(self):
        for _ in range(3):
            self._login("wrong")
        self.user.refresh_from_db()

        admin = User.objects.get(username="sec")
        lockout.unlock(self.user, by=admin)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_locked)
        self.assertEqual(self.user.failed_login_count, 0)
        entry = AuditLog.objects.get(action=AuditLog.Action.UNLOCK)
        self.assertEqual(entry.user, admin)
        self.assertEqual(self._login("correct-horse").status_code, 200)

    def test_a_dead_mail_server_cannot_prevent_the_lockout(self):
        """The notification is best-effort; the audit entry is the record of account. If a
        broken SMTP host could raise, it would become a way to stop accounts locking."""
        from unittest import mock
        with mock.patch("django.core.mail.send_mail", side_effect=OSError("no smtp")):
            for _ in range(3):
                self._login("wrong")

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_locked)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOCKOUT).exists())


class RegistrationIsClosedTests(LoginTestCase):
    def _register(self):
        return self.client.post("/api/auth/register/",
                                {"username": "stranger", "password": "hunter2hunter2"},
                                format="json")

    def test_a_stranger_cannot_create_themselves_an_account(self):
        r = self._register()
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(username="stranger").exists())

    def test_the_attempt_is_audited(self):
        """On a closed system, someone trying to make themselves an account is a security
        event, not a 403 to be discarded."""
        self._register()
        entry = AuditLog.objects.get(action=AuditLog.Action.DENIED)
        self.assertIn("registration", entry.summary.lower())
        self.assertEqual(entry.detail["username"], "stranger")

    @override_settings(DSE_ALLOW_SELF_REGISTRATION=True)
    def test_it_can_still_be_turned_on_deliberately(self):
        self.assertEqual(self._register().status_code, 201)
        self.assertTrue(User.objects.filter(username="stranger").exists())


class ThrottleTests(LoginTestCase):
    """§11.300(d) asks for safeguards that *prevent* unauthorised use, not only detect it.

    It also bounds a second problem: audit writes fail CLOSED and the trail is append-only,
    so an unthrottled attacker writes one audit row per attempt and can fill the disk.
    Detection that can be turned into a denial-of-service is not much of a control.
    """

    def test_login_attempts_are_rate_limited_per_address(self):
        User.objects.create_user("nadia", password="correct-horse")
        rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"]
        allowed = int(str(rate).split("/")[0])

        codes = [self.client.post("/api/auth/token/",
                                  {"username": "nadia", "password": "wrong"},
                                  format="json").status_code
                 for _ in range(allowed + 2)]
        self.assertIn(429, codes, f"never throttled at the shipped rate {rate}: {codes}")
        self.assertEqual(codes.count(401), allowed, "throttled too early or too late")
