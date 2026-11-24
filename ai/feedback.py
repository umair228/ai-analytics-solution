"""The feedback loop — the first signal this product has ever had about whether it is right.

There was none. No thumbs, no corrections, no accuracy number that came from a real user
asking a real question. The system could have been confidently wrong for a year and the
only way anyone would find out is a customer noticing in a meeting.

That gap also blocks everything downstream of it. ``eval/golden_egpc.jsonl`` has 21 cases
and cannot grow, because its expected answers are real EGPC ground truth that nobody here
has. But a chemist at EGPC, looking at an answer about their own laboratory, saying *"no —
PROPANE is 36%, not 3.6%"* — **that is ground truth**, and it arrives for free every time
somebody uses the product. Every demo becomes data collection.

Three rules this module keeps.

1. **A verdict is evidence, not truth.** The failure mode this entire product exists to
   prevent is a confidently-wrong answer. A chemist clicking 👍 on one is that same failure
   with a certificate attached — so a thumbs-up proves nothing, and *nothing* is promoted
   into the golden set automatically. Promotion takes a second person, deliberately.

2. **A 👎 on a certified metric is an incident, not a queue item.** See below. This is the
   most important thing in the file.

3. **Accuracy is reported per route.** "The system is 84% accurate" is a number that means
   nothing. "Certified metrics: 100%. Text-to-SQL: 78%" is an argument — it is the evidence
   for the certified layer existing at all, and it is the number that tells us where to
   spend the next month.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from core.audit import log_audit
from core.models import AuditLog

from .models import AnswerFeedback, ChatMessage

logger = logging.getLogger(__name__)


def _envelope_of(message: ChatMessage) -> dict:
    return (message.metadata or {}).get("envelope") or {}


def _question_for(message: ChatMessage) -> str:
    """The user turn that produced this answer."""
    prior = (ChatMessage.objects
             .filter(conversation_id=message.conversation_id,
                     role=ChatMessage.Role.USER,
                     created_at__lte=message.created_at)
             .order_by("-created_at", "-id").first())
    return prior.content if prior else ""


def record(*, message: ChatMessage, user, verdict: str, comment: str = "",
           expected: str = "", ip: str | None = None) -> AnswerFeedback:
    """Capture one verdict, freezing what it is a verdict *about*."""
    env = _envelope_of(message)
    prov = env.get("provenance") or {}

    fb = AnswerFeedback.objects.create(
        message=message,
        user=user if getattr(user, "is_authenticated", False) else None,
        verdict=verdict,
        comment=(comment or "").strip(),
        expected=(expected or "").strip(),
        # Frozen. The conversation can be deleted, the owner offboarded, the query re-run
        # against fresher data — and this verdict must still mean what it meant.
        question=_question_for(message),
        answer=message.content or "",
        route=str(prov.get("path") or "")[:120],
        sql=str(prov.get("sql") or ""),
        model=str(prov.get("model") or "")[:120],
        data_as_of=str(prov.get("data_as_of") or "")[:64],
    )

    log_audit(
        action=AuditLog.Action.CREATE, user=user, target_type="ai.AnswerFeedback",
        target_id=fb.pk,
        summary=f"Answer marked {verdict} ({fb.route or 'unknown route'})",
        detail={"route": fb.route, "verdict": verdict,
                "question": fb.question[:300], "expected": fb.expected[:300]},
        ip_address=ip,
    )

    if verdict == AnswerFeedback.Verdict.INCORRECT and fb.is_certified:
        _escalate_certified(fb, ip=ip)

    return fb


def _escalate_certified(fb: AnswerFeedback, *, ip=None) -> None:
    """A certified metric has been reported wrong. This is not a queue item.

    A certified metric is a hand-written, deterministic SQL definition that the router
    prefers over the language model, that dashboards and alerts are built on, and that we
    render with a ✓ Certified badge telling the user to trust it *above* the AI. If the
    definition is wrong then it is wrong **everywhere, for everyone, silently, and with our
    endorsement** — every dashboard tile, every alert threshold, every certificate of
    analysis that leaned on it.

    A wrong text-to-SQL answer is one bad answer to one person, and the router's whole
    purpose is to keep the important questions away from the model. When somebody tells us
    the safe path is the broken one, the asymmetry runs entirely the other way, and it is
    worth waking someone up for.
    """
    log_audit(
        action=AuditLog.Action.DENIED,        # a trust failure, not an ordinary event
        user=fb.user, target_type="semantics.CertifiedMetric",
        target_id=fb.route,
        summary=f"CERTIFIED METRIC REPORTED WRONG: {fb.route}",
        detail={"question": fb.question[:500], "answer": fb.answer[:500],
                "expected": fb.expected[:500], "comment": fb.comment[:500],
                "sql": fb.sql[:2000]},
        ip_address=ip,
    )
    logger.error(
        "CERTIFIED METRIC REPORTED WRONG — %s. Every dashboard, alert and certificate "
        "built on this definition is suspect until it is checked. Question: %r",
        fb.route, fb.question[:120],
    )

    recipients = _owners()
    if not recipients:
        return
    try:
        from django.core.mail import send_mail

        send_mail(
            subject=f"[DIS] A CERTIFIED metric has been reported wrong — {fb.route}",
            message=(
                f"{fb.user or 'A user'} reports that a certified metric returned the wrong "
                f"answer.\n\n"
                f"  Metric   : {fb.route}\n"
                f"  Question : {fb.question}\n"
                f"  We said  : {fb.answer[:400]}\n"
                f"  They say : {fb.expected or '(not stated)'}\n"
                f"  Comment  : {fb.comment or '(none)'}\n\n"
                f"This is not an ordinary wrong answer. A certified metric is a fixed SQL "
                f"definition that bypasses the language model, carries a ✓ Certified badge, "
                f"and is what dashboards, alerts and certificates are built on. If the "
                f"definition is wrong it is wrong everywhere, for everyone, and it is "
                f"wrong with our endorsement.\n\n"
                f"Check the definition before trusting anything built on it:\n"
                f"  semantics/specs/*.yaml   →   {fb.route.split(':', 1)[-1]}\n\n"
                f"SQL that ran:\n{fb.sql[:1500]}\n"
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=recipients,
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001
        # Best-effort. The audit entry above is the record of account, and a dead mail
        # server must not swallow the user's feedback.
        logger.exception("Could not send the certified-metric escalation for %s", fb.route)


def _owners() -> list[str]:
    from django.contrib.auth import get_user_model

    User = get_user_model()
    explicit = getattr(settings, "DSE_SECURITY_EMAILS", None)
    if explicit:
        return list(explicit)
    return list(
        User.objects.filter(is_active=True, role__in=[User.Role.ADMIN, User.Role.QA_MANAGER])
        .exclude(email="").values_list("email", flat=True))


def review(fb: AnswerFeedback, *, by, state: str, note: str = "", ip=None) -> AnswerFeedback:
    """A second person decides whether the complaint was right.

    This exists because a verdict is not truth. Somebody with the standing to say so has to
    look at the question, the SQL and the claimed correct answer, and decide — and their
    name goes on it. Only then can it become a golden-set case, which is to say: only then
    can it teach the system anything.
    """
    fb.review_state = state
    fb.reviewed_by = by
    fb.reviewed_at = timezone.now()
    fb.review_note = (note or "").strip()
    fb.save(update_fields=["review_state", "reviewed_by", "reviewed_at", "review_note",
                           "updated_at"])

    log_audit(
        action=AuditLog.Action.UPDATE, user=by, target_type="ai.AnswerFeedback",
        target_id=fb.pk,
        summary=f"Feedback reviewed: {state} ({fb.route or 'unknown route'})",
        detail={"route": fb.route, "state": state, "note": fb.review_note[:300]},
        ip_address=ip,
    )
    return fb


def accuracy_by_route(days: int = 90) -> dict:
    """Accuracy, broken down by which part of the architecture answered.

    This is the number the whole design is a bet on. If certified metrics are not measurably
    more accurate than text-to-SQL, the certified layer is ceremony and we should say so.

    ``unsure`` verdicts are counted separately and excluded from the denominator: folding
    "I don't know" into either column would be inventing a judgement nobody made.
    """
    since = timezone.now() - timezone.timedelta(days=days)
    rows = (AnswerFeedback.objects.filter(created_at__gte=since)
            .values("route")
            .annotate(
                correct=Count("pk", filter=Q(verdict=AnswerFeedback.Verdict.CORRECT)),
                incorrect=Count("pk", filter=Q(verdict=AnswerFeedback.Verdict.INCORRECT)),
                unsure=Count("pk", filter=Q(verdict=AnswerFeedback.Verdict.UNSURE)),
            ))

    def family(route: str) -> str:
        if route.startswith("certified_metric"):
            return "Certified metric"
        if route in ("text-to-sql", "sql", "nl2sql"):
            return "Text-to-SQL"
        return route or "unknown"

    grouped: dict[str, dict] = {}
    for r in rows:
        key = family(r["route"] or "")
        g = grouped.setdefault(key, {"correct": 0, "incorrect": 0, "unsure": 0})
        for k in ("correct", "incorrect", "unsure"):
            g[k] += r[k]

    out = []
    for name, g in sorted(grouped.items()):
        judged = g["correct"] + g["incorrect"]
        out.append({
            "route": name,
            **g,
            "judged": judged,
            # None, not 0 and not 100. A route nobody has judged has no accuracy, and
            # printing a number for it would be making one up.
            "accuracy": round(100 * g["correct"] / judged, 1) if judged else None,
        })

    total_judged = sum(r["judged"] for r in out)
    total_correct = sum(r["correct"] for r in out)
    return {
        "days": days,
        "routes": out,
        "overall": {
            "judged": total_judged,
            "correct": total_correct,
            "accuracy": round(100 * total_correct / total_judged, 1) if total_judged else None,
        },
        "pending_review": AnswerFeedback.objects.filter(
            verdict=AnswerFeedback.Verdict.INCORRECT,
            review_state=AnswerFeedback.Review.PENDING).count(),
        "note": ("Accuracy is over answers a human has judged, which is a biased sample — "
                 "people report what surprises them. Treat it as a floor and a trend, not "
                 "a measurement."),
    }
