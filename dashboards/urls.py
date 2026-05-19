from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import DashboardViewSet, PublicDashboardView, WidgetViewSet

router = DefaultRouter()
router.register("dashboards", DashboardViewSet, basename="dashboard")
router.register("widgets", WidgetViewSet, basename="widget")

urlpatterns = router.urls + [
    path("dashboards/public/<str:token>/", PublicDashboardView.as_view(), name="dashboard-public"),
]
