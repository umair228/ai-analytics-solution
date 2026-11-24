"""Governance controls on the AI path: data egress, determinism, evidence.

The three questions a regulated buyer asks about an LLM feature are "can it leak
our data?", "will it give the same answer twice?" and "can you show me what it
did?". These tests are the answers. SECURITY.md points here.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from ai.providers import egress
from ai.providers.anthropic_provider import AnthropicProvider
from ai.providers.base import AINotConfigured
from ai.providers.openai_provider import OpenAIProvider


# --------------------------------------------------------------------------
# Egress — "local" has to mean local
# --------------------------------------------------------------------------
class PrivateAddressTests(SimpleTestCase):
    def test_loopback_rfc1918_and_tailnet_are_private(self):
        for host in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.1.10",
                     "172.16.0.5", "100.100.100.100", "::1"):
            self.assertTrue(egress.is_private_address(host), host)

    def test_globally_routable_hosts_are_not_private(self):
        # NB 203.0.113.x and friends are NOT valid negatives here: they are
        # RFC 5737 documentation ranges, unroutable, and correctly judged private.
        for host in ("api.anthropic.com", "api.openai.com", "8.8.8.8", "1.1.1.1"):
            self.assertFalse(egress.is_private_address(host), host)

    def test_unresolvable_host_fails_closed(self):
        # An endpoint we cannot vet is one we cannot vouch for.
        self.assertFalse(egress.is_private_address("no-such-host.invalid"))


@override_settings(DSE_ALLOW_EXTERNAL_LLM=False)
class EgressBlockedByDefaultTests(SimpleTestCase):
    """The product promises lab data stays on-premise. That promise must not rest
    on a single environment variable being spelled correctly."""

    @override_settings(CLAUDE_API_KEY="sk-live-key")
    def test_anthropic_refuses_even_with_a_valid_key(self):
        provider = AnthropicProvider()
        self.assertFalse(provider.configured)
        self.assertIn("DSE_ALLOW_EXTERNAL_LLM", provider.not_configured_message())
        with self.assertRaises(AINotConfigured):
            provider._client_()

    @override_settings(LLM_BASE_URL="https://api.openai.com/v1")
    def test_local_provider_refuses_a_public_base_url(self):
        # LLM_PROVIDER=local is not a guarantee: the client underneath is a
        # generic OpenAI one and would happily talk to api.openai.com.
        provider = OpenAIProvider()
        self.assertFalse(provider.configured)
        with self.assertRaises(AINotConfigured):
            provider._client_()

    @override_settings(LLM_BASE_URL="http://127.0.0.1:11434/v1")
    def test_local_provider_accepts_an_on_prem_base_url(self):
        self.assertTrue(OpenAIProvider().configured)

    @override_settings(LLM_BASE_URL="http://100.100.100.100:11434/v1")
    def test_local_provider_accepts_the_tailnet_gpu_box(self):
        self.assertTrue(OpenAIProvider().configured)


@override_settings(DSE_ALLOW_EXTERNAL_LLM=True, CLAUDE_API_KEY="sk-live-key")
class EgressAllowedOnExplicitOptInTests(SimpleTestCase):
    def test_anthropic_is_usable_when_deliberately_permitted(self):
        self.assertTrue(AnthropicProvider().configured)


class ProviderFallbackTests(SimpleTestCase):
    """A missing or empty LLM_PROVIDER must land on "local, not configured" —
    loud and safe — never on "cloud", which is quiet and exfiltrates.

    (Asserting settings.LLM_PROVIDER itself would only prove what the developer's
    .env says. What matters is where the *fallback* goes.)
    """

    def _provider_for(self, value):
        from ai.providers.base import _CACHE, get_provider
        _CACHE.clear()
        try:
            with override_settings(LLM_PROVIDER=value):
                return get_provider()
        finally:
            _CACHE.clear()

    def test_empty_provider_falls_back_to_local(self):
        self.assertIsInstance(self._provider_for(""), OpenAIProvider)

    def test_unset_provider_falls_back_to_local(self):
        self.assertIsInstance(self._provider_for(None), OpenAIProvider)


# --------------------------------------------------------------------------
# Determinism — same question, same answer
# --------------------------------------------------------------------------
@override_settings(LLM_TEMPERATURE=0.0, LLM_SEED=42)
class DeterminismTests(SimpleTestCase):
    def test_anthropic_pins_temperature(self):
        # Previously it passed none at all, so it ran at the API default of 1.0.
        self.assertEqual(AnthropicProvider._sampling_kwargs()["temperature"], 0.0)

    def test_local_pins_temperature_and_seed(self):
        kwargs = OpenAIProvider._tuning_kwargs()
        self.assertEqual(kwargs["temperature"], 0.0)
        self.assertEqual(kwargs["seed"], 42)


# --------------------------------------------------------------------------
# Evidence — an LLM writing SQL against the LIMS is the riskiest thing we do
# --------------------------------------------------------------------------
class AskDatabaseIsAuditedTests(TestCase):
    def setUp(self):
        from connections.models import DataSource
        self.user = get_user_model().objects.create_user(username="chem", password="x")
        self.ds = DataSource.objects.create(
            name="EGPC", source_type="sqlite", database_name="/tmp/egpc.sqlite",
            options={"path": "/tmp/egpc.sqlite"}, owner=self.user)

    def _run(self, result):
        from ai.tools import _ask_database
        with mock.patch("texttosql.runner.run_text_to_sql", return_value=result):
            return _ask_database(self.user, question="how many samples went off-spec?",
                                 datasource_id=self.ds.id)

    def test_generated_sql_lands_in_the_audit_trail_with_its_content(self):
        from core.models import AuditLog

        self._run({
            "understood": True, "path": "generated", "sql": "SELECT COUNT(*) FROM SAMPLE",
            "columns": ["n"], "rows": [{"n": 12}], "row_count": 1,
            "truncated": False, "attempts": 1, "warnings": [],
        })

        entry = AuditLog.objects.get(target_type="ai.ask_database")
        self.assertEqual(entry.user, self.user)
        # An auditor must learn WHAT was asked and WHAT ran — not merely that
        # something did. The old summary was the literal string "AI chat message".
        self.assertIn("off-spec", entry.summary)
        self.assertEqual(entry.detail["sql"], "SELECT COUNT(*) FROM SAMPLE")
        self.assertEqual(entry.detail["question"], "how many samples went off-spec?")
        self.assertEqual(entry.detail["path"], "generated")
        self.assertTrue(entry.detail["understood"])

    def test_generated_sql_lands_in_query_history_beside_hand_written_sql(self):
        from querybuilder.models import QueryRun

        self._run({
            "understood": True, "path": "certified_metric:oos_rate", "sql": "SELECT 1",
            "columns": ["a"], "rows": [{"a": 1}], "row_count": 1,
            "truncated": False, "attempts": 0, "warnings": [],
        })

        run = QueryRun.objects.get(datasource=self.ds)
        self.assertEqual(run.user, self.user)
        self.assertEqual(run.sql, "SELECT 1")
        self.assertEqual(run.row_count, 1)
        self.assertEqual(run.error, "")

    def test_a_failed_answer_is_still_recorded(self):
        from core.models import AuditLog

        self._run({
            "understood": False, "path": "none",
            "error": "Could not produce a valid query after 3 attempts.",
        })

        entry = AuditLog.objects.get(target_type="ai.ask_database")
        self.assertFalse(entry.detail["understood"])
        self.assertIn("Could not produce", entry.detail["error"])

    def test_a_crash_mid_query_is_still_recorded(self):
        from ai.tools import _ask_database
        from core.models import AuditLog

        with mock.patch("texttosql.runner.run_text_to_sql", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                _ask_database(self.user, question="q", datasource_id=self.ds.id)

        # It still reached the LIMS. Absence of an answer is not absence of an act.
        entry = AuditLog.objects.get(target_type="ai.ask_database")
        self.assertFalse(entry.detail["understood"])
        self.assertIn("boom", entry.detail["error"])


# --------------------------------------------------------------------------
# Evidence retention — offboarding a user must not erase what we told them
# --------------------------------------------------------------------------
class ConversationSurvivesUserDeletionTests(TestCase):
    def test_deleting_the_owner_orphans_rather_than_destroys(self):
        from ai.models import ChatMessage, Conversation

        user = get_user_model().objects.create_user(username="leaver", password="x")
        convo = Conversation.objects.create(title="Off-spec review", owner=user)
        ChatMessage.objects.create(
            conversation=convo, role=ChatMessage.Role.ASSISTANT, content="7.9%",
            metadata={"envelope": {"provenance": {"sql": "SELECT ...", "model": "qwen2.5"}}},
        )

        user.delete()

        convo.refresh_from_db()
        self.assertIsNone(convo.owner)
        message = convo.messages.get()
        # The provenance — the generated SQL and the model that produced it — is
        # the evidence. It used to CASCADE away with the employee who left.
        self.assertEqual(message.metadata["envelope"]["provenance"]["sql"], "SELECT ...")
