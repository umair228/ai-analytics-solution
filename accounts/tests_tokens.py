"""API tokens must not be a credential the database can hand to a thief.

They were stored in plaintext, in an indexed column, looked up by equality — so database
read, or a copy of the nightly dump (which was unencrypted), was enough to impersonate
any user. And the serializer returned the plaintext on **every** list call, while its own
docstring claimed it was "only returned on creation": the intent was right and the code
did the opposite.

The property under test is simple and absolute: after a token is created, **the plaintext
exists nowhere in the system.**
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import APIToken


class TheDatabaseHoldsNoUsableCredentialTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="svc", password="x")

    def test_the_plaintext_is_never_stored(self):
        token, raw = APIToken.generate(self.user, "ci")

        self.assertTrue(raw)
        self.assertNotEqual(token.key_hash, raw)
        self.assertEqual(token.key_hash, APIToken.hash_key(raw))

        # There is no column that could hold it.
        columns = {f.name for f in APIToken._meta.get_fields()}
        self.assertNotIn("key", columns)

        # And nothing in the row resembles it.
        row = APIToken.objects.filter(pk=token.pk).values().first()
        self.assertNotIn(raw, str(row))

    def test_the_prefix_identifies_a_token_without_being_able_to_use_one(self):
        token, raw = APIToken.generate(self.user, "ci")
        self.assertEqual(token.prefix, raw[:8])
        self.assertLess(len(token.prefix), len(raw))

    def test_two_tokens_never_collide(self):
        _, a = APIToken.generate(self.user, "one")
        _, b = APIToken.generate(self.user, "two")
        self.assertNotEqual(a, b)
        self.assertEqual(APIToken.objects.count(), 2)


class AuthenticationTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="svc", password="x")
        self.token, self.raw = APIToken.generate(self.user, "ci")

    def _get(self, key):
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {key}")
        return self.client.get("/api/audit-logs/")

    def test_a_valid_token_authenticates(self):
        self.assertEqual(self._get(self.raw).status_code, 200)

    def test_the_stored_hash_is_not_itself_a_credential(self):
        """The obvious attack after a database leak: present what the database holds."""
        self.assertEqual(self._get(self.token.key_hash).status_code, 401)

    def test_a_wrong_token_is_refused(self):
        self.assertEqual(self._get("not-a-real-token").status_code, 401)

    def test_an_expired_token_is_refused_and_says_so(self):
        """A credential with no end date outlives the person, the project and the reason
        it was issued."""
        self.token.expires_at = timezone.now() - timedelta(days=1)
        self.token.save(update_fields=["expires_at"])

        r = self._get(self.raw)
        self.assertEqual(r.status_code, 401)
        self.assertIn("expired", str(r.json()).lower())

    def test_a_token_still_inside_its_window_works(self):
        self.token.expires_at = timezone.now() + timedelta(days=30)
        self.token.save(update_fields=["expires_at"])
        self.assertEqual(self._get(self.raw).status_code, 200)

    def test_deactivating_the_owner_kills_the_token(self):
        """Otherwise offboarding a leaver does nothing to their scripts."""
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self._get(self.raw).status_code, 401)


class TheApiShowsThePlaintextExactlyOnceTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="svc", password="x")
        self.client.force_authenticate(self.user)

    def test_creation_returns_the_token_with_a_warning(self):
        r = self.client.post("/api/api-tokens/", {"name": "ci"}, format="json")
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertTrue(body["key"])
        self.assertIn("cannot be shown again", body["warning"])
        # And it really is the token.
        self.assertEqual(APIToken.objects.get(pk=body["id"]).key_hash,
                         APIToken.hash_key(body["key"]))

    def test_listing_tokens_NEVER_returns_the_plaintext(self):
        """The regression that matters. `key` used to sit in `fields`, so every GET
        handed back every token in the clear."""
        created = self.client.post("/api/api-tokens/", {"name": "ci"},
                                   format="json").json()
        raw = created["key"]

        listing = self.client.get("/api/api-tokens/").json()
        rows = listing["results"] if isinstance(listing, dict) else listing

        self.assertNotIn(raw, str(rows))
        for row in rows:
            self.assertNotIn("key", row)
            self.assertIn("prefix", row)      # enough to tell them apart

    def test_retrieving_one_token_never_returns_the_plaintext(self):
        created = self.client.post("/api/api-tokens/", {"name": "ci"},
                                   format="json").json()
        one = self.client.get(f"/api/api-tokens/{created['id']}/").json()
        self.assertNotIn("key", one)
        self.assertNotIn(created["key"], str(one))

    def test_a_user_cannot_see_another_users_tokens(self):
        other = get_user_model().objects.create_user(username="other", password="x")
        APIToken.generate(other, "theirs")
        self.client.post("/api/api-tokens/", {"name": "mine"}, format="json")

        listing = self.client.get("/api/api-tokens/").json()
        rows = listing["results"] if isinstance(listing, dict) else listing
        self.assertEqual([r["name"] for r in rows], ["mine"])
