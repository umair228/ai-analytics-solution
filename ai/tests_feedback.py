"""The feedback loop — and the ways it could quietly make things worse.

This is the first signal the product has ever had about whether it is *right*. It is also,
handled carelessly, a way to teach the system to be wrong on purpose: a single mistaken
thumbs-down, promoted straight into the golden set, becomes a permanent false fact that
every future accuracy measurement is graded against.

So the tests that matter here are not "does the button work". They are:

* a thumbs-up proves nothing and promotes nothing;
* nobody can uphold their own complaint;
* you cannot judge an answer that was never given to you;
* a 👎 on a **certified metric** is escalated, because a wrong certified definition is
  wrong everywhere, silently, and with our endorsement;
* a verdict survives the deletion of the thing it was about.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from rest_framework.test import APITestCase

from ai.models import AnswerFeedback, ChatMessage, Conversation
from core.models import AuditLog

User = get_user_model()


def envelope(path, sql="SELECT 1", model="qwen2.5:7b"):
    return {"envelope": {"provenance": {"path": path, "sql": sql, "model": model,
                                        "data_as_of": "2026-07-13T06:00:00Z"}}}


class FeedbackBase(APITestCase):
    def setUp(self):
        self.chem = User.objects.create_user("nadia", password="x",
                                             role=User.Role.ANALYST)
        self.mgr = User.objects.create_user("mgr", password="x",
                                            role=User.Role.LAB_MANAGER,
                                            email="mgr@lab.local")
        User.objects.create_user("qa", password="x", role=User.Role.QA_MANAGER,
                                 email="qa@lab.local")

        self.convo = Conversation.objects.create(owner=self.chem, title="OOS")
        ChatMessage.objects.create(conversation=self.convo, role=ChatMessage.Role.USER,
                                   content="Which product has the highest OOS rate?")
        self.answer = ChatMessage.objects.create(
            conversation=self.convo, role=ChatMessage.Role.ASSISTANT,
            content="PROPANE, at about 3.6%.",
            metadata=envelope("text-to-sql"))

    def _submit(self, user, **body):
        self.client.force_authenticate(user)
        return self.client.post("/api/ai/feedback/",
                                {"message_id": self.answer.id, **body}, format="json")


class GivingAVerdictTests(FeedbackBase):
    def test_a_verdict_freezes_what_it_is_a_verdict_about(self):
        """The conversation can be deleted, the owner offboarded, the query re-run against
        fresher data — and the judgement must still mean what it meant."""
        self._submit(self.chem, verdict="incorrect",
                     expected="PROPANE is ~36%, not 3.6%.")
        fb = AnswerFeedback.objects.get()

        self.assertEqual(fb.question, "Which product has the highest OOS rate?")
        self.assertEqual(fb.answer, "PROPANE, at about 3.6%.")
        self.assertEqual(fb.route, "text-to-sql")
        self.assertEqual(fb.sql, "SELECT 1")
        self.assertEqual(fb.model, "qwen2.5:7b")

    def test_the_verdict_survives_the_answer_being_deleted(self):
        self._submit(self.chem, verdict="incorrect", expected="~36%")
        self.convo.delete()          # takes the ChatMessages with it

        fb = AnswerFeedback.objects.get()
        self.assertIsNone(fb.message)
        self.assertEqual(fb.question, "Which product has the highest OOS rate?")
        self.assertEqual(fb.expected, "~36%")

    def test_you_cannot_judge_an_answer_you_were_never_given(self):
        """A verdict on a conversation you never saw is not evidence — it is a way to poison
        the golden set."""
        stranger = User.objects.create_user("other", password="x",
                                            role=User.Role.ANALYST)
        self.assertEqual(self._submit(stranger, verdict="incorrect").status_code, 403)
        self.assertEqual(AnswerFeedback.objects.count(), 0)

    def test_a_nonsense_verdict_is_refused(self):
        self.assertEqual(self._submit(self.chem, verdict="lgtm").status_code, 400)

    def test_the_verdict_is_audited(self):
        self._submit(self.chem, verdict="correct")
        self.assertTrue(AuditLog.objects.filter(
            target_type="ai.AnswerFeedback").exists())


class ATumbsUpIsNotGroundTruthTests(FeedbackBase):
    """The failure this product exists to prevent is a confidently-wrong answer. A chemist
    clicking 👍 on one is that same failure with a certificate attached."""

    def test_a_thumbs_up_promotes_nothing(self):
        self._submit(self.chem, verdict="correct")
        fb = AnswerFeedback.objects.get()
        self.assertEqual(fb.review_state, AnswerFeedback.Review.PENDING)
        self.assertFalse(fb.exported_to_golden)

    def test_even_an_accepted_complaint_is_not_exported_until_somebody_exports_it(self):
        self._submit(self.chem, verdict="incorrect", expected="~36%")
        fb = AnswerFeedback.objects.get()
        self.assertFalse(fb.exported_to_golden)


class ReviewTests(FeedbackBase):
    def _report(self):
        self._submit(self.chem, verdict="incorrect", expected="PROPANE is ~36%.")
        return AnswerFeedback.objects.get()

    def _review(self, user, fb, state="accepted", note=""):
        self.client.force_authenticate(user)
        return self.client.post(f"/api/ai/feedback/{fb.id}/review/",
                                {"state": state, "note": note}, format="json")

    def test_you_cannot_uphold_your_own_complaint(self):
        """Otherwise the review step is theatre, and one person's surprise becomes a fact
        the system is permanently graded against."""
        fb = self._report()
        self.assertEqual(self._review(self.chem, fb).status_code, 403)

        fb.refresh_from_db()
        self.assertEqual(fb.review_state, AnswerFeedback.Review.PENDING)

    def test_an_analyst_cannot_review_at_all(self):
        fb = self._report()
        other = User.objects.create_user("chem2", password="x", role=User.Role.ANALYST)
        self.assertEqual(self._review(other, fb).status_code, 403)

    def test_a_manager_can_uphold_it_and_their_name_goes_on_it(self):
        fb = self._report()
        self.assertEqual(self._review(self.mgr, fb, note="Confirmed against the LIMS.")
                         .status_code, 200)

        fb.refresh_from_db()
        self.assertEqual(fb.review_state, AnswerFeedback.Review.ACCEPTED)
        self.assertEqual(fb.reviewed_by, self.mgr)
        self.assertIsNotNone(fb.reviewed_at)
        self.assertEqual(fb.review_note, "Confirmed against the LIMS.")

    def test_a_manager_can_dismiss_it(self):
        """Sometimes the system was right and the user was mistaken. That is a finding too,
        and it must be recordable — otherwise the only way to close a complaint is to agree
        with it."""
        fb = self._report()
        self._review(self.mgr, fb, state="rejected", note="The answer was right.")
        fb.refresh_from_db()
        self.assertEqual(fb.review_state, AnswerFeedback.Review.REJECTED)

    def test_the_queue_shows_complaints_not_compliments(self):
        self._report()
        self._submit(self.chem, verdict="correct")

        self.client.force_authenticate(self.mgr)
        rows = self.client.get("/api/ai/feedback/queue/").json()["results"]
        self.assertEqual([r["verdict"] for r in rows], ["incorrect"])

    def test_an_analyst_cannot_see_the_queue(self):
        self.client.force_authenticate(self.chem)
        self.assertEqual(self.client.get("/api/ai/feedback/queue/").status_code, 403)


class ACertifiedMetricBeingWrongIsAnIncidentTests(FeedbackBase):
    """Not a queue item. A certified metric is a fixed SQL definition that bypasses the
    model, carries a ✓ Certified badge, and is what dashboards, alerts and certificates are
    built on. If it is wrong, it is wrong everywhere, for everyone, silently, and with our
    endorsement."""

    def setUp(self):
        super().setUp()
        self.answer.metadata = envelope("certified_metric:oos_rate_by_product")
        self.answer.save(update_fields=["metadata"])

    def test_it_is_recognised_as_certified(self):
        self._submit(self.chem, verdict="incorrect", expected="~36%")
        self.assertTrue(AnswerFeedback.objects.get().is_certified)

    def test_it_raises_a_loud_audit_entry_naming_the_metric(self):
        self._submit(self.chem, verdict="incorrect", expected="~36%")
        entry = AuditLog.objects.get(target_type="semantics.CertifiedMetric")
        self.assertIn("CERTIFIED METRIC REPORTED WRONG", entry.summary)
        self.assertEqual(entry.target_id, "certified_metric:oos_rate_by_product")

    def test_it_is_mailed_to_the_people_who_own_the_definitions(self):
        self._submit(self.chem, verdict="incorrect", expected="~36%")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("CERTIFIED", mail.outbox[0].subject)
        self.assertIn("qa@lab.local", mail.outbox[0].to)

    def test_the_user_is_told_their_report_was_escalated(self):
        body = self._submit(self.chem, verdict="incorrect", expected="~36%").json()
        self.assertIn("escalated", body["escalated"].lower())

    def test_a_thumbs_UP_on_a_certified_metric_escalates_nothing(self):
        self._submit(self.chem, verdict="correct")
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(AuditLog.objects.filter(
            target_type="semantics.CertifiedMetric").exists())

    def test_an_ordinary_text_to_sql_miss_is_NOT_escalated(self):
        """The router's whole purpose is to keep the important questions away from the
        model. A model getting one wrong is the expected cost of that design, not an
        incident."""
        self.answer.metadata = envelope("text-to-sql")
        self.answer.save(update_fields=["metadata"])

        self._submit(self.chem, verdict="incorrect", expected="~36%")
        self.assertEqual(len(mail.outbox), 0)

    def test_the_certified_failure_sorts_to_the_top_of_the_queue(self):
        # An ordinary miss, reported first.
        plain = ChatMessage.objects.create(
            conversation=self.convo, role=ChatMessage.Role.ASSISTANT,
            content="Something else.", metadata=envelope("text-to-sql"))
        self.client.force_authenticate(self.chem)
        self.client.post("/api/ai/feedback/",
                         {"message_id": plain.id, "verdict": "incorrect"}, format="json")
        # Then the certified one.
        self._submit(self.chem, verdict="incorrect", expected="~36%")

        self.client.force_authenticate(self.mgr)
        rows = self.client.get("/api/ai/feedback/queue/").json()["results"]
        self.assertTrue(rows[0]["is_certified"], "the incident is not at the top")


class AccuracyByRouteTests(FeedbackBase):
    """"The system is 84% accurate" means nothing. "Certified: 100%, text-to-SQL: 78%" is an
    argument — and it is the evidence for the certified layer existing at all."""

    def _answer_with(self, path, content="x"):
        return ChatMessage.objects.create(
            conversation=self.convo, role=ChatMessage.Role.ASSISTANT,
            content=content, metadata=envelope(path))

    def _judge(self, msg, verdict):
        self.client.force_authenticate(self.chem)
        self.client.post("/api/ai/feedback/",
                         {"message_id": msg.id, "verdict": verdict}, format="json")

    def test_accuracy_is_reported_per_route(self):
        for _ in range(3):
            self._judge(self._answer_with("certified_metric:oos"), "correct")
        self._judge(self._answer_with("text-to-sql"), "correct")
        self._judge(self._answer_with("text-to-sql"), "incorrect")

        self.client.force_authenticate(self.chem)
        body = self.client.get("/api/ai/accuracy/").json()
        by = {r["route"]: r for r in body["routes"]}

        self.assertEqual(by["Certified metric"]["accuracy"], 100.0)
        self.assertEqual(by["Text-to-SQL"]["accuracy"], 50.0)

    def test_an_unsure_verdict_is_not_folded_into_either_column(self):
        """Counting "I don't know" as either right or wrong is inventing a judgement nobody
        made."""
        self._judge(self._answer_with("text-to-sql"), "correct")
        self._judge(self._answer_with("text-to-sql"), "unsure")

        self.client.force_authenticate(self.chem)
        route = self.client.get("/api/ai/accuracy/").json()["routes"][0]
        self.assertEqual(route["judged"], 1)
        self.assertEqual(route["unsure"], 1)
        self.assertEqual(route["accuracy"], 100.0)

    def test_a_route_nobody_has_judged_has_NO_accuracy_not_zero_and_not_a_hundred(self):
        self.client.force_authenticate(self.chem)
        body = self.client.get("/api/ai/accuracy/").json()
        self.assertIsNone(body["overall"]["accuracy"])

    def test_it_says_out_loud_that_the_sample_is_biased(self):
        """People report what surprises them. A number derived from that is a floor and a
        trend, not a measurement, and the page must not pretend otherwise."""
        self.client.force_authenticate(self.chem)
        self.assertIn("biased", self.client.get("/api/ai/accuracy/").json()["note"])
