"""Turn reviewed feedback into golden-set cases.

``eval/golden_egpc.jsonl`` has 21 cases and I have not been able to grow it, because its
expected answers are **real EGPC ground truth** — PROPANE at ~36% OOS, analyst 5227 with 310
off-spec results — derived from a database I do not have. Inventing more would be precisely
the confidently-wrong number this entire product exists to eliminate.

But a chemist at EGPC, looking at an answer about their own laboratory and saying *"no —
it's 36%, not 3.6%"*, **is** that ground truth. It arrives for free every time somebody uses
the product, and it is about the questions they actually ask rather than the ones we guessed
they would.

    python manage.py export_golden --dry-run     # see what would be added
    python manage.py export_golden               # append the reviewed cases

**Only cases a second person reviewed and ACCEPTED are exported.** Never a raw complaint.
A user reporting an answer as wrong is evidence; a manager confirming it, with their name
against it, is what makes it safe to teach the system. Skipping that step would let one
mistaken thumbs-down permanently teach the product a false fact — and the whole reason this
product is defensible is that we do not do that.
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from ai.models import AnswerFeedback
from core.audit import log_audit
from core.models import AuditLog


class Command(BaseCommand):
    help = "Append reviewed-and-accepted feedback to the golden evaluation set."

    def add_arguments(self, parser):
        parser.add_argument("--out", default=str(Path(settings.BASE_DIR) / "eval" /
                                                 "golden_egpc.jsonl"))
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        path = Path(opts["out"])
        pending = list(
            AnswerFeedback.objects
            .filter(review_state=AnswerFeedback.Review.ACCEPTED,
                    exported_to_golden=False)
            .exclude(question="").exclude(expected="")
            .select_related("reviewed_by"))

        if not pending:
            self.stdout.write(
                "Nothing to export. A case is eligible only once someone has REVIEWED the "
                "complaint and accepted it, and stated what the right answer was — an "
                "unreviewed thumbs-down is one person's surprise, not a fact.")
            self._report_ineligible()
            return

        existing = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    existing.add(json.loads(line).get("question", "").strip().lower())
                except ValueError:
                    continue

        cases, skipped = [], 0
        for fb in pending:
            if fb.question.strip().lower() in existing:
                skipped += 1
                continue
            cases.append({
                "id": f"fb{fb.pk}",
                "section": "From the laboratory",
                "capability": "nl_sql",
                "answerable": "yes",
                "question": fb.question.strip(),
                "expected_answer": fb.expected.strip(),
                # No `expect_keywords`: guessing which words must appear is how a grader
                # comes to reward the shape of an answer instead of its correctness. A
                # human wrote the expected answer; leave the grading to whatever compares
                # against it.
                "source": (f"User feedback #{fb.pk}, reviewed by "
                           f"{getattr(fb.reviewed_by, 'username', 'unknown')} on "
                           f"{fb.reviewed_at:%Y-%m-%d}" if fb.reviewed_at else "user feedback"),
                "route_that_failed": fb.route or "unknown",
            })

        self.stdout.write(f"{len(cases)} new case(s); {skipped} already in the set.")
        for c in cases:
            self.stdout.write(f"  + {c['question'][:70]}")
            self.stdout.write(self.style.SUCCESS(f"      -> {c['expected_answer'][:70]}"))

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDry run — nothing written."))
            return
        if not cases:
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for c in cases:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")

        ids = [int(c["id"][2:]) for c in cases]
        AnswerFeedback.objects.filter(pk__in=ids).update(
            exported_to_golden=True, updated_at=timezone.now())

        # Changing the set the system is graded against is a change to the definition of
        # correct. It does not get to happen quietly.
        log_audit(
            action=AuditLog.Action.UPDATE, target_type="eval.GoldenSet", target_id=path.name,
            summary=f"{len(cases)} case(s) added to the golden set from reviewed feedback",
            detail={"questions": [c["question"][:200] for c in cases]},
        )
        self.stdout.write(self.style.SUCCESS(
            f"\nAppended {len(cases)} case(s) to {path}. Re-run the accuracy gate."))

    def _report_ineligible(self):
        """Say what is waiting and why, rather than just "nothing to do"."""
        unreviewed = AnswerFeedback.objects.filter(
            verdict=AnswerFeedback.Verdict.INCORRECT,
            review_state=AnswerFeedback.Review.PENDING).count()
        no_expected = AnswerFeedback.objects.filter(
            review_state=AnswerFeedback.Review.ACCEPTED, expected="").count()
        if unreviewed:
            self.stdout.write(self.style.WARNING(
                f"  {unreviewed} reported-wrong answer(s) are waiting for review."))
        if no_expected:
            self.stdout.write(self.style.WARNING(
                f"  {no_expected} accepted complaint(s) never said what the RIGHT answer "
                f"was, so there is nothing to grade against."))
