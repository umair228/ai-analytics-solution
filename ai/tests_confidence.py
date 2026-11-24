"""Confidence — and the chip that was quietly lying.

The product rendered **"Generated SQL — validated before it ran"** on every generated answer.
That is true, and a customer hears *"the answer is right"*. It means: read-only parse, table
allow-list, sanity checks. Not one of those says the query answered **the question that was
asked** — a model that writes a beautifully valid `SELECT avg(density)` when you asked about
*sulphur* passes every gate we have, first time, with no warnings.

Meanwhile the runner was already counting how many times the model's SQL failed validation
and had to be corrected, and already collecting the post-execution sanity violations — and
the envelope **threw both away**. An answer the model got right first time and one it got
wrong twice arrived on screen identical.

So the tests here are about honesty, not arithmetic:

* a shaky answer must not look like a confident one;
* a certified answer must not look like a generated one;
* zero rows must not be silently read as "the answer is zero";
* stale data must lower the confidence even when the SQL is perfect;
* and there must be **no percentage anywhere** — a fake number is the confidently-wrong
  answer this whole product exists to eliminate, and putting one on the screen and calling
  it rigour would be the worst thing in the codebase.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from ai import confidence
from ai.envelope import envelope_from_result
from connections.models import DataSource
from semantics.models import CertifiedMetric

User = get_user_model()


def answer(**kw):
    base = {
        "understood": True, "path": "generated", "sql": "SELECT 1",
        "columns": ["n"], "rows": [[1]], "row_count": 1,
        "attempts": 1, "warnings": [],
    }
    return {**base, **kw}


class TheEvidenceWeWereThrowingAwayTests(TestCase):
    def test_a_first_try_answer_is_confident(self):
        c = confidence.grade(answer(attempts=1))
        self.assertEqual(c["level"], confidence.HIGH)

    def test_an_answer_the_model_got_wrong_twice_is_NOT(self):
        """`attempts=3` means two of the model's queries failed validation and were sent
        back. The validator only ever checks that SQL is SAFE, never that it is RIGHT — so
        this is the model working the question out as it goes, and the user was never told."""
        c = confidence.grade(answer(attempts=3))
        self.assertEqual(c["level"], confidence.MEDIUM)
        self.assertIn("failed validation", " ".join(c["reasons"]))

    def test_a_sanity_warning_lowers_it(self):
        c = confidence.grade(answer(warnings=["The join multiplied your rows 40x."]))
        self.assertEqual(c["level"], confidence.MEDIUM)
        self.assertIn("multiplied", " ".join(c["reasons"]))

    def test_retried_AND_suspicious_is_low(self):
        c = confidence.grade(answer(attempts=3, warnings=["That aggregate looks impossible."]))
        self.assertEqual(c["level"], confidence.LOW)

    def test_zero_rows_is_flagged_because_it_is_AMBIGUOUS(self):
        """"There is nothing to find" and "I asked the wrong question" produce the identical
        empty table, and nothing in the product distinguished them."""
        c = confidence.grade(answer(row_count=0, rows=[]))
        self.assertEqual(c["level"], confidence.LOW)
        self.assertIn("no rows", " ".join(c["reasons"]).lower())


class ACertifiedAnswerIsNotAGeneratedOneTests(TestCase):
    def test_certified_is_its_own_level(self):
        c = confidence.grade(answer(path="certified_metric:oos_rate_by_product"))
        self.assertEqual(c["level"], confidence.CERTIFIED)
        self.assertIn("wrote none of it", " ".join(c["reasons"]))

    def test_it_is_a_fact_about_the_ROUTE_not_an_estimate(self):
        """A certified metric is hand-written, QA-reviewed SQL. Saying so is not a
        confidence guess — it is a statement about which code ran."""
        c = confidence.grade(answer(path="certified_metric:x", attempts=1))
        self.assertNotIn("%", str(c))

    def test_STALE_data_pulls_even_a_certified_answer_down(self):
        """Stale data does not make the query wrong. It makes the ANSWER wrong, and a
        perfect query over three-day-old numbers is still a wrong answer to "what is it
        now"."""
        c = confidence.grade(answer(
            path="certified_metric:x", freshness="stale",
            freshness_detail="The replica last synced 3 days ago."))
        self.assertEqual(c["level"], confidence.MEDIUM)
        self.assertIn("3 days", " ".join(c["reasons"]))


class ThereIsNoFakePercentageTests(TestCase):
    """"A calibrated chip plus an honest refusal is a stronger trust move than a fake
    percentage" — and a fake percentage is exactly the confidently-wrong number this product
    exists to eliminate."""

    def test_no_grade_anywhere_carries_a_number(self):
        for kw in (
            {}, {"attempts": 4}, {"row_count": 0, "rows": []},
            {"path": "certified_metric:x"}, {"understood": False},
            {"warnings": ["odd"]}, {"freshness": "stale"},
        ):
            c = confidence.grade(answer(**kw))
            blob = str(c)
            self.assertNotIn("%", blob)
            self.assertNotIn("score", blob.lower())
            self.assertNotIn("probability", blob.lower())

    def test_the_source_declares_it(self):
        """A guard on the intent, not just the output — the next person to reach for a
        softmax should hit this."""
        import pathlib

        src = pathlib.Path(confidence.__file__).read_text().lower()
        self.assertIn("never a percentage", src)


class WhenWeDoNotKnowWeSaySoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("nadia", password="x")
        self.ds = DataSource.objects.create(name="EGPC LIMS", owner=self.user,
                                            source_type="mssql")
        CertifiedMetric.objects.create(
            datasource=self.ds, key="oos_rate_by_product",
            name="Out-of-specification rate by product",
            description="Share of results outside spec, per product.",
            sql="SELECT 1")

    def test_a_refusal_is_its_own_level_and_says_do_not_treat_this_as_an_answer(self):
        c = confidence.grade({"understood": False})
        self.assertEqual(c["level"], confidence.NONE)
        self.assertIn("do not treat", c["trust"].lower())

    def test_a_refusal_offers_what_we_CAN_answer(self):
        """A refusal that stops at "I don't know" is a dead end, and in a demo it is an
        embarrassment. One that says "not that — but I can tell you these, exactly, from
        approved SQL" is the product's whole argument, made at the moment it is most
        persuasive."""
        env = envelope_from_result({
            "understood": False, "path": "none",
            "datasource": {"id": self.ds.id, "name": self.ds.name},
            "error": "Could not turn that into a query.",
        }).as_dict()

        self.assertEqual(env["confidence"]["level"], confidence.NONE)
        self.assertEqual([m["key"] for m in env["can_answer"]], ["oos_rate_by_product"])
        self.assertIn("Out-of-specification", env["can_answer"][0]["name"])

    def test_a_SUCCESSFUL_answer_does_not_carry_the_consolation_list(self):
        env = envelope_from_result(answer(
            datasource={"id": self.ds.id, "name": self.ds.name})).as_dict()
        self.assertEqual(env["can_answer"], [])


class TheEnvelopeActuallyCarriesItTests(TestCase):
    """The whole bug was that it did not."""

    def test_attempts_and_warnings_reach_the_client(self):
        env = envelope_from_result(answer(
            attempts=3, warnings=["That aggregate looks impossible."])).as_dict()

        self.assertEqual(env["provenance"]["attempts"], 3)
        self.assertIn("impossible", str(env["provenance"]["warnings"]))
        # Retried AND suspicious. Either alone is "check this"; both together is "do not
        # rely on this" — and neither was visible at all before.
        self.assertEqual(env["confidence"]["level"], confidence.LOW)

    def test_the_confidence_block_is_always_present(self):
        for result in (answer(), answer(understood=False), {}):
            env = envelope_from_result(result).as_dict()
            self.assertIn("level", env["confidence"])
            self.assertIn("label", env["confidence"])
            self.assertTrue(env["confidence"]["reasons"])
