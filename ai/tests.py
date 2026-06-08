"""Tests for the agentic core: provider message translation, the agent loop,
and the tool registry. The loop tests use a scripted mock provider + stubbed
tools, so they run with no network and no live LLM."""
from unittest import mock

from django.test import SimpleTestCase

from ai.agent import run_agent
from ai.providers.anthropic_provider import AnthropicProvider
from ai.providers.openai_provider import OpenAIProvider
from ai.tools import TOOLS, execute_tool, tool_schemas


# --------------------------------------------------------------------------
# Provider neutral→native message translation
# --------------------------------------------------------------------------
NEUTRAL = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "let me check",
     "tool_calls": [{"id": "c1", "name": "list_datasets", "input": {}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "list_datasets",
     "content": '{"count": 2}'},
]


class ProviderTranslationTests(SimpleTestCase):
    def test_anthropic_merges_tool_results_into_user_turn(self):
        out = AnthropicProvider._to_anthropic_messages(NEUTRAL + [
            {"role": "tool", "tool_call_id": "c2", "name": "x", "content": "{}"},
        ])
        # user, assistant(tool_use), then ONE user turn holding both tool_results
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[1]["role"], "assistant")
        tool_use = [b for b in out[1]["content"] if b["type"] == "tool_use"]
        self.assertEqual(tool_use[0]["id"], "c1")
        self.assertEqual(out[2]["role"], "user")
        results = [b for b in out[2]["content"] if b["type"] == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["tool_use_id"], "c1")

    def test_openai_maps_tool_calls_and_results(self):
        out = OpenAIProvider._to_tool_messages(
            [{"text": "SYS"}], NEUTRAL
        )
        self.assertEqual(out[0], {"role": "system", "content": "SYS"})
        assistant = out[2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "list_datasets")
        # arguments must be a JSON *string*
        self.assertIsInstance(assistant["tool_calls"][0]["function"]["arguments"], str)
        self.assertEqual(out[3], {"role": "tool", "tool_call_id": "c1",
                                  "content": '{"count": 2}'})


# --------------------------------------------------------------------------
# Tool registry
# --------------------------------------------------------------------------
class ToolRegistryTests(SimpleTestCase):
    def test_schemas_are_well_formed(self):
        schemas = tool_schemas()
        self.assertEqual(len(schemas), len(TOOLS))
        for s in schemas:
            self.assertTrue(s["name"])
            self.assertTrue(s["description"])
            self.assertEqual(s["input_schema"]["type"], "object")

    def test_unknown_tool_fails_soft(self):
        out = execute_tool("nope", object(), {})
        self.assertIn("error", out)

    def test_missing_dataset_fails_soft(self):
        # No DB row → ToolError caught → {"error": ...}, never raises.
        out = execute_tool("describe_dataset", _FakeUser(), {"dataset_id": 999999})
        self.assertIn("error", out)


class _FakeUser:
    is_admin = False
    id = 1
    pk = 1


# --------------------------------------------------------------------------
# Agent loop — scripted provider + stubbed tools (no network, no DB)
# --------------------------------------------------------------------------
class _ScriptedProvider:
    """Returns pre-baked turns and records what it was asked each call."""
    name = "mock"
    configured = True

    def __init__(self, turns):
        self._turns = list(turns)
        self.seen = []

    def not_configured_message(self):
        return "n/a"

    def complete(self, *, system_blocks, messages, tools, max_tokens):
        self.seen.append({
            "roles": [m["role"] for m in messages],
            "tool_names": [t["name"] for t in tools],
        })
        return self._turns.pop(0)


def _tool_call(cid, name, **inp):
    return {"id": cid, "name": name, "input": inp}


class AgentLoopTests(SimpleTestCase):
    def _run(self, turns, tool_result=None, **kw):
        provider = _ScriptedProvider(turns)
        stub = mock.Mock(return_value=tool_result or {"ok": True})
        with mock.patch("ai.agent.get_provider", return_value=provider), \
             mock.patch("ai.agent.execute_tool", stub):
            run = run_agent(_FakeUser(), [{"role": "user", "content": "q"}], **kw)
        return run, provider, stub

    def test_answers_directly_with_no_tools(self):
        run, provider, stub = self._run([{"text": "42", "tool_calls": []}])
        self.assertEqual(run.answer, "42")
        self.assertEqual(run.tool_calls, 0)
        self.assertEqual(run.stopped_reason, "answered")
        stub.assert_not_called()

    def test_runs_tool_then_answers_and_threads_result_back(self):
        run, provider, stub = self._run(
            turns=[
                {"text": "checking", "tool_calls": [_tool_call("c1", "list_datasets")]},
                {"text": "done: 5 datasets", "tool_calls": []},
            ],
            tool_result={"count": 5},
        )
        self.assertEqual(run.answer, "done: 5 datasets")
        self.assertEqual(run.tool_calls, 1)
        self.assertEqual(len(run.trace), 1)
        self.assertEqual(run.trace[0]["tool"], "list_datasets")
        self.assertTrue(run.trace[0]["ok"])
        # The SECOND model call must have seen the tool result threaded in.
        self.assertIn("tool", provider.seen[1]["roles"])

    def test_recovers_tool_call_written_as_text(self):
        # Local models often emit the call as TEXT instead of a native tool_call;
        # the loop must recover it and run the tool, not quit with the preamble.
        run, provider, stub = self._run(
            turns=[
                {"text": 'Let me check.\n```\n{"name": "list_datasets", "arguments": {}}\n```',
                 "tool_calls": []},
                {"text": "done: 5 datasets", "tool_calls": []},
            ],
            tool_result={"count": 5},
        )
        self.assertEqual(run.answer, "done: 5 datasets")
        self.assertEqual(run.tool_calls, 1)
        self.assertEqual(run.trace[0]["tool"], "list_datasets")
        stub.assert_called_once()

    def test_tool_error_is_recorded_and_recoverable(self):
        run, provider, stub = self._run(
            turns=[
                {"text": "", "tool_calls": [_tool_call("c1", "aggregate_data")]},
                {"text": "recovered", "tool_calls": []},
            ],
            tool_result={"error": "bad column"},
        )
        self.assertEqual(run.answer, "recovered")
        self.assertFalse(run.trace[0]["ok"])

    def test_stops_at_max_steps_and_forces_an_answer(self):
        # Every step asks for a tool; the loop must cap and force a final answer.
        keep_calling = {"text": "more", "tool_calls": [_tool_call("c", "list_datasets")]}
        run, provider, stub = self._run(
            turns=[keep_calling, keep_calling, {"text": "final", "tool_calls": []}],
            max_steps=2,
        )
        self.assertEqual(run.stopped_reason, "max_steps")
        self.assertEqual(run.answer, "final")
        self.assertEqual(run.tool_calls, 2)
        # Final forced call must offer NO tools.
        self.assertEqual(provider.seen[-1]["tool_names"], [])
