"""Answer feedback over HTTP.

Who may do what, and why:

* **Anyone may judge an answer they were given.** Feedback is worth having in proportion to
  how easy it is to leave, and the person who asked the question is the person who knows
  whether it was right. They may only judge answers in *their own* conversations — a
  verdict on a conversation you never saw is not evidence of anything.

* **Only a manager or an administrator may REVIEW a complaint.** A verdict is evidence, not
  truth; deciding that the system really was wrong — and that the case should teach it
  something — is a judgement with authority behind it, and the reviewer's name goes on it.
  If the person who complained could also uphold their own complaint, the review step would
  be theatre.

* **Accuracy is visible to any authenticated user.** It is the honest number. If it is bad,
  the people using the system are exactly who should know.
"""
from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.audit import client_ip

from . import feedback as fb_service
from .models import AnswerFeedback, ChatMessage


class AnswerFeedbackSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True, default=None)
    reviewer = serializers.CharField(source="reviewed_by.username", read_only=True,
                                     default=None)
    is_certified = serializers.BooleanField(read_only=True)

    class Meta:
        model = AnswerFeedback
        fields = [
            "id", "verdict", "comment", "expected", "question", "answer", "route",
            "sql", "model", "data_as_of", "is_certified",
            "review_state", "reviewer", "reviewed_at", "review_note",
            "exported_to_golden", "username", "created_at",
        ]
        read_only_fields = fields


def _is_manager(user) -> bool:
    return bool(getattr(user, "is_admin", False) or getattr(user, "is_manager", False))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit(request):
    """Judge one answer.  Body: {message_id, verdict, comment?, expected?}"""
    message_id = request.data.get("message_id")
    verdict = request.data.get("verdict")

    if verdict not in AnswerFeedback.Verdict.values:
        return Response(
            {"error": True,
             "detail": f"verdict must be one of {list(AnswerFeedback.Verdict.values)}."},
            status=status.HTTP_400_BAD_REQUEST)

    message = ChatMessage.objects.filter(
        pk=message_id, role=ChatMessage.Role.ASSISTANT).select_related(
        "conversation").first()
    if message is None:
        return Response({"error": True, "detail": "No such answer."},
                        status=status.HTTP_404_NOT_FOUND)

    # You may judge answers you were given. A verdict on a conversation you never saw is
    # not evidence of anything, and it is a way to poison the golden set.
    owner_id = message.conversation.owner_id
    if owner_id != request.user.id and not getattr(request.user, "is_admin", False):
        return Response({"error": True, "detail": "That is not your conversation."},
                        status=status.HTTP_403_FORBIDDEN)

    entry = fb_service.record(
        message=message, user=request.user, verdict=verdict,
        comment=request.data.get("comment") or "",
        expected=request.data.get("expected") or "",
        ip=client_ip(request),
    )
    body = AnswerFeedbackSerializer(entry).data
    if entry.verdict == AnswerFeedback.Verdict.INCORRECT and entry.is_certified:
        body["escalated"] = (
            "This answer came from a CERTIFIED metric — a fixed SQL definition that "
            "dashboards, alerts and certificates are built on. Your report has been "
            "escalated, because if that definition is wrong it is wrong everywhere.")
    return Response(body, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def queue(request):
    """Reported-wrong answers awaiting a decision. Managers and administrators."""
    if not _is_manager(request.user):
        return Response({"error": True, "detail": "Laboratory manager or administrator."},
                        status=status.HTTP_403_FORBIDDEN)

    qs = AnswerFeedback.objects.select_related("user", "reviewed_by")
    state = request.query_params.get("state", "pending")
    if state != "all":
        qs = qs.filter(review_state=state)
    if request.query_params.get("verdict"):
        qs = qs.filter(verdict=request.query_params["verdict"])
    else:
        # The queue is for complaints. A thumbs-up needs nobody's attention.
        qs = qs.filter(verdict=AnswerFeedback.Verdict.INCORRECT)

    # Certified-metric failures first, always. They are not the same kind of event.
    rows = sorted(qs[:200],
                  key=lambda f: (not f.is_certified, -f.pk))
    return Response({"results": AnswerFeedbackSerializer(rows, many=True).data,
                     "count": qs.count()})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def review(request, pk):
    """Uphold or dismiss a complaint.  Body: {state: accepted|rejected, note?}"""
    if not _is_manager(request.user):
        return Response({"error": True, "detail": "Laboratory manager or administrator."},
                        status=status.HTTP_403_FORBIDDEN)

    entry = AnswerFeedback.objects.filter(pk=pk).first()
    if entry is None:
        return Response({"error": True, "detail": "No such feedback."},
                        status=status.HTTP_404_NOT_FOUND)

    state = request.data.get("state")
    if state not in (AnswerFeedback.Review.ACCEPTED, AnswerFeedback.Review.REJECTED):
        return Response({"error": True, "detail": "state must be accepted or rejected."},
                        status=status.HTTP_400_BAD_REQUEST)

    # The reviewer must not be the complainant. Upholding your own complaint is not a
    # review, and this is the only thing standing between "a user was surprised" and "the
    # system will now be taught that it was wrong".
    if entry.user_id == request.user.id and not getattr(request.user, "is_superuser", False):
        return Response(
            {"error": True,
             "detail": "You reported this answer. Someone else has to review it."},
            status=status.HTTP_403_FORBIDDEN)

    fb_service.review(entry, by=request.user, state=state,
                      note=request.data.get("note") or "", ip=client_ip(request))
    return Response(AnswerFeedbackSerializer(entry).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def accuracy(request):
    """Accuracy per route — the number the whole architecture is a bet on."""
    try:
        days = max(1, min(int(request.query_params.get("days", 90)), 3650))
    except (TypeError, ValueError):
        days = 90
    return Response(fb_service.accuracy_by_route(days))
