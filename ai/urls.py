from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ConversationViewSet,
    agent_chat,
    ai_status,
    ask_live,
    chat,
    export_answer,
    insights,
    save_dataset,
    widget_suggest,
)

router = DefaultRouter()
router.register("ai/conversations", ConversationViewSet, basename="conversation")

urlpatterns = [
    path("ai/status/", ai_status, name="ai-status"),
    path("ai/chat/", chat, name="ai-chat"),
    path("ai/agent/", agent_chat, name="ai-agent"),
    path("ai/ask-live/", ask_live, name="ai-ask-live"),
    path("ai/export/", export_answer, name="ai-export"),
    path("ai/save-dataset/", save_dataset, name="ai-save-dataset"),
    path("ai/insights/", insights, name="ai-insights"),
    path("ai/widget-suggest/", widget_suggest, name="ai-widget-suggest"),
] + router.urls
