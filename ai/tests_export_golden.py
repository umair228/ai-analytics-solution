from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from ai.models import AnswerFeedback, ChatMessage, Conversation
User = get_user_model()


class ExportGoldenTests(TestCase):
    """The blocker this whole loop exists to break: golden_egpc.jsonl cannot grow without
    real EGPC ground truth, and a chemist correcting an answer IS real EGPC ground truth."""

    def setUp(self):
        self.chem = User.objects.create_user("nadia", role=User.Role.ANALYST)
        self.mgr = User.objects.create_user("mgr", role=User.Role.LAB_MANAGER)
        c = Conversation.objects.create(owner=self.chem)
        ChatMessage.objects.create(conversation=c, role="user",
                                   content="Which product has the highest OOS rate?")
        self.msg = ChatMessage.objects.create(
            conversation=c, role="assistant", content="PROPANE, about 3.6%.",
            metadata={"envelope": {"provenance": {"path": "text-to-sql"}}})

    def _fb(self, **kw):
        from ai import feedback
        return feedback.record(message=self.msg, user=self.chem, verdict="incorrect",
                               expected="PROPANE is highest at ~36% OOS.", **kw)

    def test_an_UNREVIEWED_complaint_is_never_exported(self):
        self._fb()
        with TemporaryDirectory() as d:
            out = Path(d) / "g.jsonl"
            buf = StringIO()
            call_command("export_golden", out=str(out), stdout=buf)
            self.assertFalse(out.exists())
            self.assertIn("waiting for review", buf.getvalue())

    def test_a_REVIEWED_and_accepted_complaint_becomes_a_golden_case(self):
        from ai import feedback
        fb = self._fb()
        feedback.review(fb, by=self.mgr, state="accepted", note="Checked in the LIMS.")

        with TemporaryDirectory() as d:
            out = Path(d) / "g.jsonl"
            buf = StringIO()
            call_command("export_golden", out=str(out), stdout=buf)

            case = json.loads(out.read_text().strip())
            self.assertEqual(case["question"], "Which product has the highest OOS rate?")
            self.assertEqual(case["expected_answer"], "PROPANE is highest at ~36% OOS.")
            self.assertIn("mgr", case["source"])
            self.assertEqual(case["route_that_failed"], "text-to-sql")

        fb.refresh_from_db()
        self.assertTrue(fb.exported_to_golden)

    def test_it_is_not_exported_twice(self):
        from ai import feedback
        fb = self._fb()
        feedback.review(fb, by=self.mgr, state="accepted")
        with TemporaryDirectory() as d:
            out = Path(d) / "g.jsonl"
            call_command("export_golden", out=str(out), stdout=StringIO())
            call_command("export_golden", out=str(out), stdout=StringIO())
            self.assertEqual(len(out.read_text().strip().splitlines()), 1)

    def test_a_REJECTED_complaint_is_never_exported(self):
        """The user was mistaken and the system was right. Teaching it otherwise would be
        the worst thing this feature could do."""
        from ai import feedback
        fb = self._fb()
        feedback.review(fb, by=self.mgr, state="rejected", note="The answer was right.")
        with TemporaryDirectory() as d:
            out = Path(d) / "g.jsonl"
            call_command("export_golden", out=str(out), stdout=StringIO())
            self.assertFalse(out.exists())

    def test_an_accepted_complaint_with_no_stated_right_answer_is_not_exported(self):
        from ai import feedback
        fb = self._fb()
        fb.expected = ""
        fb.save()
        feedback.review(fb, by=self.mgr, state="accepted")
        with TemporaryDirectory() as d:
            out = Path(d) / "g.jsonl"
            buf = StringIO()
            call_command("export_golden", out=str(out), stdout=buf)
            self.assertFalse(out.exists())
            self.assertIn("never said what the RIGHT answer", buf.getvalue())
