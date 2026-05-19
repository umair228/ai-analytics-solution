from rest_framework.routers import DefaultRouter

from .views import AlertEventViewSet, AlertViewSet, DatasetViewSet

router = DefaultRouter()
router.register("datasets", DatasetViewSet, basename="dataset")
router.register("alerts", AlertViewSet, basename="alert")
router.register("alert-events", AlertEventViewSet, basename="alert-event")

urlpatterns = router.urls
