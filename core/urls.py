from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AuditLogViewSet, branding_view, client_export_audit, health

router = DefaultRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = [
    path("health/", health, name="health"),
    path("branding/", branding_view, name="branding"),
    path("audit/client-export/", client_export_audit, name="client-export-audit"),
    *router.urls,
]
