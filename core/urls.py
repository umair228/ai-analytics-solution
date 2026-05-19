from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AuditLogViewSet, health

router = DefaultRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = [
    path("health/", health, name="health"),
    *router.urls,
]
