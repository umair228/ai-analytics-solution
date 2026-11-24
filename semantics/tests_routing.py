"""The certified-metric router, scored against the routing golden set.

This is the deterministic accuracy gate. It needs no LLM and no database — the router
is a pure function of the question and the curated layer — so unlike every other
accuracy claim in this product, it can run on every push and stay true.

The asymmetry is the whole design, and these tests encode it:

* **Precision must be 1.0.** The certified path bypasses the LLM *and* the validator.
  A metric that fires when it should not hands the user a confidently-wrong
  deterministic answer with a ✓ Certified badge on it. "How many results are off-spec
  LAST MONTH" answered with the all-time total, stamped certified, is the worst thing
  this system can do. Every point below 1.0 is a certified lie.

* **Recall may be below 1.0.** A metric that should fire and doesn't merely falls
  through to the long-tail path — which is validated, constraint-aware and
  self-repairing. The user gets a correct answer, slightly slower. That is a cost, not
  a defect, and it is the correct trade.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from semantics.eval_routing import evaluate, layer_from_spec, load_cases
from semantics.router import match_certified_metric

# The acceptance criterion. Precision is not negotiable; recall is a target.
MIN_PRECISION = 1.0
MIN_RECALL = 0.90


class RoutingGoldenSetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = evaluate()

    def test_precision_is_perfect(self):
        """Not 'high'. Perfect. A certified metric that fires on a question it cannot
        honour is worse than no certified metric at all."""
        bad = [r for r in self.report["results"]
               if r["outcome"] in ("false_positive", "wrong_metric")]
        detail = "\n  ".join(
            f"{r['id']}: {r['question']!r}\n     expected {r['expect']!r}, "
            f"got {r['got']!r}\n     {r['why']}"
            for r in bad)
        self.assertEqual(
            self.report["precision"], MIN_PRECISION,
            f"\n{len(bad)} question(s) were answered by a certified metric that should "
            f"not have answered them:\n  {detail}")

    def test_recall_meets_the_target(self):
        self.assertGreaterEqual(
            self.report["recall"], MIN_RECALL,
            "Certified metrics are not firing on their own canonical phrasings — "
            "questions that should be instant and deterministic are being sent to the "
            "language model instead. Widen the synonyms in egpc_dev.yaml.")

    def test_no_dangerous_outcome(self):
        self.assertEqual(self.report["dangerous"], 0)

    def test_the_golden_set_is_big_enough_to_mean_anything(self):
        """A precision figure over a dozen questions is not a measurement, it is an
        anecdote."""
        cases = load_cases()
        self.assertGreaterEqual(len(cases), 50)

    def test_the_golden_set_is_mostly_adversarial(self):
        """Positive cases prove the router works on the phrasings we wrote for it,
        which is the easy half. The hard half — and the one that protects the user —
        is the questions it must REFUSE to answer."""
        cases = load_cases()
        must_defer = sum(1 for c in cases if c.get("expect") is None)
        self.assertGreater(must_defer, len(cases) * 0.4)

    def test_every_case_explains_itself(self):
        for c in load_cases():
            self.assertGreater(len(c.get("why", "")), 10,
                               f"{c['id']} gives no reason for its expectation")


class TheDangerousCasesTests(SimpleTestCase):
    """Spelled out individually, because these are the ones that must never regress."""

    def setUp(self):
        self.layer = layer_from_spec()

    def _route(self, q):
        m = match_certified_metric(self.layer, q)
        return m.key if m else None

    def test_a_time_window_defers_rather_than_silently_answering_all_time(self):
        # The plain question fires…
        self.assertEqual(self._route("how many off spec results"), "oos_total")
        # …and the same question with a time window must NOT, because the certified
        # SQL has no WHERE clause for it and would answer a different question.
        self.assertIsNone(self._route("how many off spec results last month"))
        self.assertIsNone(self._route("how many off spec results this year"))

    def test_a_rate_question_and_a_count_question_do_not_cross_fire(self):
        """'Which product has the worst off-spec RATE' and 'which product has the most
        off-spec RESULTS' are different questions with different answers — a small
        product with a few failures tops one and not the other."""
        self.assertEqual(self._route("off spec rate by product"), "oos_rate_by_product")
        self.assertEqual(self._route("most off spec results by product"),
                         "oos_by_product_count")

    def test_a_dimension_with_no_certified_metric_defers(self):
        """EGPC's TEST.INSTRUMENT is essentially empty and SAMPLE.CUSTOMER is ~93%
        empty. Answering these from the product metric would be silently wrong."""
        for q in ("which instrument has the most off spec results",
                  "off spec rate by customer",
                  "off spec rate by lab section"):
            self.assertIsNone(self._route(q), q)

    def test_an_uncertified_analyte_defers_rather_than_returning_sulphur(self):
        self.assertEqual(self._route("average sulphur by product"), "sulphur_by_product")
        self.assertIsNone(self._route("average benzene by product"))

    def test_smalltalk_never_fires_a_metric(self):
        for q in ("hello", "what can you do", "thanks"):
            self.assertIsNone(self._route(q), q)
