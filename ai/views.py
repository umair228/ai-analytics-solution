from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.audit import record_audit
from core.models import AuditLog
from datasets.models import Dataset

from .agent import run_agent
from .client import (
    AINotConfigured,
    active_model,
    generate_insights,
    is_configured,
    run_chat,
    suggest_widgets,
)
from .context import build_dataset_context
from .models import ChatMessage, Conversation
from .serializers import ConversationListSerializer, ConversationSerializer

_NOT_CONFIGURED = Response(
    {"error": True, "detail": "The AI assistant is not configured."},
    status=status.HTTP_503_SERVICE_UNAVAILABLE,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ai_status(request):
    """Whether the AI assistant is available (drives the frontend)."""
    return Response({
        "configured": is_configured(),
        "model": active_model(),
    })


class ConversationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """List, read and delete the current user's AI conversations."""

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            Conversation.objects.filter(owner=self.request.user)
            .select_related("dataset")
            .prefetch_related("messages")
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ConversationSerializer
        return ConversationListSerializer


def _accessible_dataset(request, dataset_id):
    if not dataset_id:
        return None
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset and dataset.accessible_by(request.user):
        return dataset
    return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat(request):
    """Send a message to the AI assistant within a (new or existing) conversation."""
    if not is_configured():
        return _NOT_CONFIGURED

    message = (request.data.get("message") or "").strip()
    if not message:
        return Response({"error": True, "detail": "A message is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    conversation_id = request.data.get("conversation_id")
    if conversation_id:
        conversation = get_object_or_404(
            Conversation, pk=conversation_id, owner=request.user
        )
    else:
        conversation = Conversation.objects.create(
            owner=request.user,
            dataset=_accessible_dataset(request, request.data.get("dataset")),
            title=message[:60],
        )

    ChatMessage.objects.create(
        conversation=conversation, role=ChatMessage.Role.USER, content=message
    )
    history = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages.all()
    ]

    dataset_context = None
    if conversation.dataset:
        try:
            dataset_context = build_dataset_context(conversation.dataset)
        except Exception:  # noqa: BLE001 - dataset context is best-effort
            dataset_context = None

    try:
        reply = run_chat(history, dataset_context)
    except AINotConfigured as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"AI request failed: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    ChatMessage.objects.create(
        conversation=conversation, role=ChatMessage.Role.ASSISTANT, content=reply
    )
    conversation.save(update_fields=["updated_at"])
    record_audit(request, AuditLog.Action.QUERY, target_type="Conversation",
                 target_id=conversation.id, summary="AI chat message")

    return Response({
        "conversation_id": conversation.id,
        "reply": reply,
        "conversation": ConversationSerializer(conversation).data,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def agent_chat(request):
    """Agentic chat: the assistant investigates the user's data/documents with
    tools and returns a grounded answer plus the evidence trace it used.

    Same request shape as ``chat`` (message, optional conversation_id, optional
    dataset). The assistant reply's ``metadata`` carries the tool-call trace.
    """
    if not is_configured():
        return _NOT_CONFIGURED

    message = (request.data.get("message") or "").strip()
    if not message:
        return Response({"error": True, "detail": "A message is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    conversation_id = request.data.get("conversation_id")
    if conversation_id:
        conversation = get_object_or_404(
            Conversation, pk=conversation_id, owner=request.user
        )
    else:
        conversation = Conversation.objects.create(
            owner=request.user,
            dataset=_accessible_dataset(request, request.data.get("dataset")),
            title=message[:60],
        )

    ChatMessage.objects.create(
        conversation=conversation, role=ChatMessage.Role.USER, content=message
    )
    history = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages.all()
    ]

    try:
        run = run_agent(request.user, history, dataset=conversation.dataset)
    except AINotConfigured as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"Agent run failed: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    meta = {
        "trace": run.trace,
        "steps": run.steps,
        "tool_calls": run.tool_calls,
        "stopped_reason": run.stopped_reason,
    }
    ChatMessage.objects.create(
        conversation=conversation, role=ChatMessage.Role.ASSISTANT,
        content=run.answer, metadata=meta,
    )
    conversation.save(update_fields=["updated_at"])
    record_audit(
        request, AuditLog.Action.QUERY, target_type="Conversation",
        target_id=conversation.id,
        summary=f"AI agent run ({run.tool_calls} tool calls)",
    )

    return Response({
        "conversation_id": conversation.id,
        "reply": run.answer,
        "trace": run.trace,
        "steps": run.steps,
        "tool_calls": run.tool_calls,
        "stopped_reason": run.stopped_reason,
        "conversation": ConversationSerializer(conversation).data,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def insights(request):
    """Generate a natural-language insights brief for a dataset."""
    if not is_configured():
        return _NOT_CONFIGURED

    dataset = _accessible_dataset(request, request.data.get("dataset"))
    if dataset is None:
        return Response({"error": True, "detail": "Dataset not found or access denied."},
                        status=status.HTTP_404_NOT_FOUND)

    try:
        dataset_context = build_dataset_context(dataset)
        text = generate_insights(dataset_context)
    except AINotConfigured as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"AI request failed: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    record_audit(request, AuditLog.Action.QUERY, target_type="Dataset",
                 target_id=dataset.id, summary=f"AI insights for '{dataset.name}'")
    return Response({"dataset": dataset.id, "insights": text})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def widget_suggest(request):
    """Suggest chart configurations for a dataset (provider-agnostic)."""
    if not is_configured():
        return _NOT_CONFIGURED

    dataset = _accessible_dataset(request, request.data.get("dataset"))
    if dataset is None:
        return Response(
            {"error": True, "detail": "Dataset not found or access denied."},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        context = build_dataset_context(dataset)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"Could not load dataset: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    try:
        suggestions = suggest_widgets(context)
    except AINotConfigured as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"AI request failed: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    record_audit(request, AuditLog.Action.QUERY, target_type="Dataset",
                 target_id=dataset.id, summary=f"AI widget suggestions for '{dataset.name}'")
    return Response({"dataset": dataset.id, "suggestions": suggestions})
