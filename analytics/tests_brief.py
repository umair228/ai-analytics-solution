"""The nightly Lab Brief — the first thing DIS does without being asked.

Everything else in the product is pull. Nothing has ever run on its own behalf, which
is the whole difference between a tool and a colleague.

The two tests that matter: the brief must still send when the LLM is down (a digest
that only arrives when the GPU is up is not a digest), and it must NOT manufacture
urgency when there is none (a digest that cries wolf nightly gets filtered to a folder
within a fortnight).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from analytics import brief
from connections.models import DataSource
from semantics.spec import apply_spec, load_yaml

SPEC = Path(__file__).resolve().parent.parent / "semantics" / "specs" / "egpc_dev.yaml"


class _LabBase(TestCase):
    def setUp(self):
        cache.clear()
        from semantics import layer as layer_mod
        layer_mod.invalidate()

        self.tmp = tempfile.TemporaryDirectory()
        db = str(Path(self.tmp.name) / "lims.sqlite")
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE sample (sample_number INTEGER, product TEXT, sample_type TEXT,
              status TEXT, in_spec TEXT, login_date TEXT, date_completed TEXT);
            CREATE TABLE result (sample_number INTEGER, in_spec TEXT);
            INSERT INTO sample VALUES
              (1,'Diesel','SCHEDULED','C','F',date('now','-2 days'),date('now','-1 days')),
              (2,'Diesel','UNSCHEDULED','I','T',date('now','-1 days'),NULL);
            INSERT INTO result VALUES (1,'F'), (1,'T');
        """)
        con.commit(); con.close()

        self.user = get_user_model().objects.create_user(
            username="lab.manager", password="x", role="lab_manager",
            email="mgr@lab.local")
        self.ds = DataSource.objects.create(
            name="EGPC", source_type="sqlite", database_name=db,
            options={"path": db}, owner=self.user)
        apply_spec(self.ds, load_yaml(SPEC), verify=False)

    def tearDown(self):
        self.tmp.cleanup()
        cache.clear()


class FactsAreRealTests(_LabBase):
    def test_the_brief_is_built_from_certified_numbers_not_invented_ones(self):
        facts = brief.gather(self.user)
        self.assertTrue(facts["configured"])
        tiles = {t["key"]: t for t in facts["tiles"]}

        # 50% off-spec against a 2% target — a breach, and it must be identified as one.
        self.assertEqual(tiles["off_spec_rate_30d"]["value"], 50.0)
        self.assertIn("off_spec_rate_30d", [t["key"] for t in facts["breaches"]])
        self.assertEqual(facts["freshness"], "live")


class NarrationTests(_LabBase):
    def test_the_brief_still_sends_when_the_LLM_is_down(self):
        """A digest that only arrives when the GPU is up is not a digest."""
        from ai.providers.base import AINotConfigured

        with mock.patch("ai.client.run_chat", side_effect=AINotConfigured("no model")):
            b = brief.build(self.user)

        self.assertTrue(b["body"])
        self.assertIn("Off-spec rate", b["body"])
        self.assertIn("50.0%", b["body"])          # the real number, from the template

    def test_the_model_narrates_but_never_supplies_a_number(self):
        captured = {}

        def fake_chat(messages, **kw):
            captured["prompt"] = messages[0]["content"]
            return "• Off-spec rate is 50% against a 2% target."

        with mock.patch("ai.client.run_chat", side_effect=fake_chat):
            b = brief.build(self.user)

        prompt = captured["prompt"]
        # Every figure it could possibly need is handed to it…
        self.assertIn("Off-spec rate: 50.0%", prompt)
        # …and it is told, explicitly, not to make any up.
        self.assertIn("Do not compute, estimate, extrapolate or invent", prompt)
        self.assertIn("UNAVAILABLE is unknown, NOT zero", prompt)
        self.assertIn("50%", b["body"])


class QuietTests(_LabBase):
    def test_a_healthy_lab_is_reported_as_quiet_and_not_emailed(self):
        """A digest that manufactures urgency every night to justify itself gets
        filtered to a folder within a fortnight."""
        # Loosen the target so nothing breaches.
        from semantics.models import HealthTile
        from semantics import layer as layer_mod
        HealthTile.objects.filter(datasource=self.ds).update(target=None,
                                                             direction="neutral")
        layer_mod.invalidate()
        cache.clear()

        b = brief.build(self.user)
        self.assertTrue(b["quiet"])
        self.assertIn("all clear", b["subject"])

        mail.outbox.clear()
        call_command("send_lab_brief", verbosity=0)
        self.assertEqual(len(mail.outbox), 0)      # nothing to say → say nothing

        call_command("send_lab_brief", "--send-quiet", verbosity=0)
        self.assertEqual(len(mail.outbox), 1)      # …unless asked

    def test_a_lab_with_a_breach_is_emailed(self):
        mail.outbox.clear()
        with mock.patch("ai.client.run_chat", side_effect=RuntimeError("llm down")):
            call_command("send_lab_brief", verbosity=0)

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["mgr@lab.local"])
        self.assertIn("need attention", msg.subject)
        self.assertIn("Off-spec rate", msg.body)
        # Provenance travels with the brief, as it does with every other number.
        self.assertIn("Figures come from certified SQL", msg.body)
        self.assertIn("freshness", msg.body)


class RecipientTests(_LabBase):
    def test_it_goes_to_people_who_can_act_on_it(self):
        get_user_model().objects.create_user(
            username="an", password="x", role="analyst", email="an@lab.local")
        mail.outbox.clear()
        with mock.patch("ai.client.run_chat", side_effect=RuntimeError("llm down")):
            call_command("send_lab_brief", verbosity=0)

        recipients = {r for m in mail.outbox for r in m.to}
        self.assertIn("mgr@lab.local", recipients)
        # An analyst does not need a nightly digest of the whole lab.
        self.assertNotIn("an@lab.local", recipients)

    def test_the_hour_gate_no_ops_cheaply(self):
        mail.outbox.clear()
        call_command("send_lab_brief", "--hour", "99", verbosity=0)
        self.assertEqual(len(mail.outbox), 0)
