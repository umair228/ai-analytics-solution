from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ConversationViewSet, ai_status, chat, insights, widget_suggest

router = DefaultRouter()
router.register("ai/conversations", ConversationViewSet, basename="conversation")

urlpatterns = [
    path("ai/status/", ai_status, name="ai-status"),
    path("ai/chat/", chat, name="ai-chat"),
    path("ai/insights/", insights, name="ai-insights"),
    path("ai/widget-suggest/", widget_suggest, name="ai-widget-suggest"),
] + router.urls
