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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ask_live(request):
    """Talk → Visualize → Report: run the agent over the user's question and return
    one AnswerEnvelope (narrative + result table + a deterministic chart spec + full
    provenance). The frontend renders the narrative + chart and can export the same
    envelope via /ai/export/.  Body: {message, conversation_id?, dataset?}."""
    from .envelope import envelope_from_agent_run

    if not is_configured():
        return _NOT_CONFIGURED
    message = (request.data.get("message") or "").strip()
    if not message:
        return Response({"error": True, "detail": "A message is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    conversation_id = request.data.get("conversation_id")
    if conversation_id:
        conversation = get_object_or_404(Conversation, pk=conversation_id, owner=request.user)
    else:
        conversation = Conversation.objects.create(
            owner=request.user,
            dataset=_accessible_dataset(request, request.data.get("dataset")),
            title=message[:60])

    ChatMessage.objects.create(
        conversation=conversation, role=ChatMessage.Role.USER, content=message)
    history = [{"role": m.role, "content": m.content} for m in conversation.messages.all()]

    try:
        run = run_agent(request.user, history, dataset=conversation.dataset)
    except AINotConfigured as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"Agent run failed: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    envelope = envelope_from_agent_run(run, model=active_model(), user_id=request.user.id)
    ChatMessage.objects.create(
        conversation=conversation, role=ChatMessage.Role.ASSISTANT,
        content=run.answer, metadata={"trace": run.trace, "envelope": envelope.as_dict()})
    conversation.save(update_fields=["updated_at"])
    record_audit(request, AuditLog.Action.QUERY, target_type="Conversation",
                 target_id=conversation.id, summary="AI ask-live (envelope)")

    return Response({"conversation_id": conversation.id, "envelope": envelope.as_dict()})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def export_answer(request):
    """Render an AnswerEnvelope to a downloadable report. Body: {envelope, format}.
    The client posts the envelope it already holds, so exports are deterministic
    (no re-run). Formats: csv, xlsx, html, pdf (pdf needs WeasyPrint server-side)."""
    from django.http import HttpResponse

    from .exporters import ExportUnavailable, export

    envelope = request.data.get("envelope")
    if not isinstance(envelope, dict):
        return Response({"error": True, "detail": "An 'envelope' object is required."},
                        status=status.HTTP_400_BAD_REQUEST)
    fmt = (request.data.get("format") or "csv").lower()
    try:
        data, content_type, filename = export(envelope, fmt)
    except ExportUnavailable as exc:
        return Response({"error": True, "detail": str(exc)},
                        status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001
        return Response({"error": True, "detail": f"Export failed: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)

    record_audit(request, AuditLog.Action.QUERY, target_type="AnswerEnvelope",
                 target_id=None, summary=f"Exported answer as {fmt}")
    resp = HttpResponse(data, content_type=content_type)
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def save_dataset(request):
    """Turn an Ask-the-Database answer's SQL into a reusable, dashboard-able Dataset
    (the 'report / operationalize' bridge). Body: {sql, datasource_id, name}. Creates
    a raw QueryDefinition + Dataset, refreshes it, and returns the dataset id — after
    which the normal dataset/dashboard machinery applies."""
    from connections.models import DataSource
    from datasets.models import Dataset
    from datasets.services import refresh_dataset
    from querybuilder.executor import QueryError, assert_read_only
    from querybuilder.models import QueryDefinition

    sql = (request.data.get("sql") or "").strip()
    name = ((request.data.get("name") or "").strip() or "Saved answer")[:200]
    ds_id = request.data.get("datasource_id") or request.data.get("datasource")
    if not sql:
        return Response({"error": True, "detail": "A 'sql' string is required."},
                        status=status.HTTP_400_BAD_REQUEST)
    ds = DataSource.objects.filter(pk=ds_id).first() if ds_id else None
    if ds is None or not ds.accessible_by(request.user):
        return Response({"error": True, "detail": "Unknown or inaccessible datasource."},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        assert_read_only(sql)
    except QueryError as exc:
        return Response({"error": True, "detail": f"Only a read-only SELECT can be saved: {exc}"},
                        status=status.HTTP_400_BAD_REQUEST)

    query = QueryDefinition.objects.create(
        name=name, description="Saved from an Ask-the-Database answer.",
        datasource=ds, mode=QueryDefinition.Mode.RAW, raw_sql=sql, generated_sql=sql,
        owner=request.user, visibility=QueryDefinition.Visibility.SHARED)
    dataset = Dataset.objects.create(
        name=name, description="Saved from an Ask-the-Database answer.",
        query=query, owner=request.user, visibility=Dataset.Visibility.SHARED)
    row_count = None
    try:
        row_count = refresh_dataset(dataset).get("row_count")
    except Exception as exc:  # noqa: BLE001 - dataset is created; surface refresh issues softly
        dataset.last_error = str(exc)[:2000]
        dataset.save(update_fields=["last_error", "updated_at"])

    record_audit(request, AuditLog.Action.CREATE, target_type="Dataset",
                 target_id=dataset.id, summary=f"Saved dataset from answer: {name}")
    return Response({"dataset_id": dataset.id, "name": dataset.name, "row_count": row_count})
