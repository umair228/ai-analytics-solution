from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class Conversation(TimeStampedModel):
    """A chat session with the AI assistant, optionally bound to a dataset."""

    title = models.CharField(max_length=200, default="New conversation")
    # SET_NULL, not CASCADE: a ChatMessage carries the agent trace and the
    # AnswerEnvelope (the generated SQL, the path taken, the model). That is
    # evidence. Offboarding a user must not erase the record of what the system
    # told them — AuditLog.user makes the same choice for the same reason.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="conversations",
    )
    dataset = models.ForeignKey(
        "datasets.Dataset", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="conversations",
    )

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class ChatMessage(TimeStampedModel):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()

    # For agent answers: the evidence trace (tool calls + results) and run stats.
    # Empty for ordinary single-shot chat messages.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.role}: {self.content[:48]}"


class AnswerFeedback(TimeStampedModel):
    """A human verdict on one answer — the only signal we have ever had about whether the
    system is *right*.

    Until now there was none. Not a thumbs-up, not a correction, nothing: the product could
    be confidently wrong for a year and the only way anyone would find out is a customer
    noticing. Every accuracy lever downstream of this — growing the golden set, tuning the
    router, fine-tuning the embedder — needs ground truth about real questions, and a
    domain expert saying "no, and here is the right answer" *is* ground truth.

    Three things this model is careful about.

    **A verdict is evidence, not truth.** The failure this whole product exists to prevent
    is a confidently-wrong answer; a chemist clicking 👍 on one is that same failure with a
    certificate attached. So nothing here is ever promoted automatically. Getting into the
    golden set takes a second person, deliberately, and the model records who.

    **The verdict is frozen at the moment it is given.** Question, answer, SQL, route and
    model are *copied*, not referenced. A conversation can be deleted, an owner offboarded
    (``Conversation.owner`` is SET_NULL for exactly this reason), a query re-run against
    fresher data — and the verdict has to survive all of it, still meaning what it meant.
    A judgement that quietly re-points at a different answer is worse than no judgement.

    **A 👎 on a certified metric is not the same event as a 👎 on text-to-SQL**, and
    :mod:`ai.feedback` treats them differently. See ``is_certified``.
    """

    class Verdict(models.TextChoices):
        CORRECT = "correct", "Correct"
        INCORRECT = "incorrect", "Incorrect"
        UNSURE = "unsure", "Not sure"

    class Review(models.TextChoices):
        PENDING = "pending", "Awaiting review"
        ACCEPTED = "accepted", "Accepted — the answer was wrong"
        REJECTED = "rejected", "Rejected — the answer was right"

    message = models.ForeignKey(
        ChatMessage, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="feedback",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="answer_feedback",
    )
    verdict = models.CharField(max_length=12, choices=Verdict.choices)
    comment = models.TextField(blank=True)
    # What the right answer *is*. This is the field that becomes a golden-set case, and it
    # is the whole reason a thumbs-down is worth more than a thumbs-up.
    expected = models.TextField(blank=True)

    # ---- frozen at the moment of the verdict ----------------------------------
    question = models.TextField(blank=True)
    answer = models.TextField(blank=True)
    # "certified_metric:oos_rate_by_product" | "text-to-sql" | "agent" — WHICH PART of the
    # architecture answered. Accuracy broken down by this is the evidence for the certified
    # layer existing at all.
    route = models.CharField(max_length=120, blank=True)
    sql = models.TextField(blank=True)
    model = models.CharField(max_length=120, blank=True)
    data_as_of = models.CharField(max_length=64, blank=True)

    # ---- review ---------------------------------------------------------------
    review_state = models.CharField(max_length=12, choices=Review.choices,
                                    default=Review.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="feedback_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    exported_to_golden = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["route", "verdict"]),
            models.Index(fields=["review_state"]),
        ]

    @property
    def is_certified(self) -> bool:
        """Did a *certified metric* produce this answer?

        It matters enormously. A certified metric is a hand-written, deterministic SQL
        definition that dashboards and alerts are built on, and it ships with a ✓ Certified
        badge telling people to trust it above everything else. If it is wrong, it is wrong
        **everywhere, for everyone, silently, and with our endorsement**. A text-to-SQL miss
        is one bad answer to one person.

        So a negative verdict here is not a queue item. It is an incident.
        """
        return self.route.startswith("certified_metric")

    def __str__(self):
        return f"{self.verdict} — {self.question[:40]}"
