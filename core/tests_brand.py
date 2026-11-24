"""One product, one name.

The app shipped with five: "Data Science Engine" (browser title, sidebar alt text),
"DSE" (alert emails, the chat system prompt), "Interactive Reporting & AI Analytics
Solution" (the health endpoint), "DIS — Decision Intelligence Suite" (the logo, the
architecture doc, the demo script) and "DIS Assistant / DIS Agent / DIS AI Assistant".

The worst of them was in the system prompt. `ai/client.py` told the model *"You are
DSE Assistant, embedded in Data Science Engine"* while `ai/agent.py` told it *"You are
the DIS Analytics Agent"* — so the assistant introduced itself with a **different
product name depending on which code path the request happened to take**, underneath a
logo that reads "Decision Intelligence Suite". Ask it who it is in a demo and you get
the wrong answer, confidently.

The product is DIS. These tests keep it that way, because brand drift is silent: no
test fails, nothing 500s, and the first person to notice is a customer.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from rest_framework.test import APITestCase

BRAND = "DIS"
FORBIDDEN = ("DSE Assistant", "Data Science Engine")


class SystemPromptsAgreeOnTheProductNameTests(SimpleTestCase):
    def test_the_chat_prompt_says_DIS(self):
        from ai.client import SYSTEM_PROMPT

        self.assertIn("DIS", SYSTEM_PROMPT)
        for bad in FORBIDDEN:
            self.assertNotIn(bad, SYSTEM_PROMPT)

    def test_the_agent_prompt_says_DIS(self):
        from ai.agent import AGENT_SYSTEM_PROMPT

        self.assertIn("DIS", AGENT_SYSTEM_PROMPT)
        for bad in FORBIDDEN:
            self.assertNotIn(bad, AGENT_SYSTEM_PROMPT)

    def test_both_paths_introduce_the_SAME_product(self):
        """The bug this file exists for: the assistant's identity must not depend on
        which code path the request took."""
        from ai.agent import AGENT_SYSTEM_PROMPT
        from ai.client import SYSTEM_PROMPT

        self.assertIn("DIS", SYSTEM_PROMPT)
        self.assertIn("DIS", AGENT_SYSTEM_PROMPT)

    def test_the_canned_greeting_says_DIS(self):
        """"Who are you?" is the first thing anyone asks in a demo, and it takes the
        no-LLM fast path — so the wrong name here is the one a customer hears first."""
        from ai.agent import _smalltalk_fast_path

        run = _smalltalk_fast_path([{"role": "user", "content": "hello"}])
        self.assertIsNotNone(run, "a greeting must take the smalltalk fast path")
        self.assertIn("DIS", run.answer)
        for bad in FORBIDDEN:
            self.assertNotIn(bad, run.answer)


class OutwardFacingSurfacesTests(APITestCase):
    def test_the_health_endpoint_names_the_product(self):
        body = self.client.get("/api/health/").json()
        self.assertEqual(body["service"], "dis-api")
        self.assertIn(BRAND, body["name"])
