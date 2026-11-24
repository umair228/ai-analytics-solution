from rest_framework.routers import DefaultRouter

from .views import AlertEventViewSet, AlertViewSet, DatasetReportViewSet, DatasetViewSet
from .views_saved import SavedViewViewSet

router = DefaultRouter()
router.register("datasets", DatasetViewSet, basename="dataset")
router.register("alerts", AlertViewSet, basename="alert")
router.register("alert-events", AlertEventViewSet, basename="alert-event")
router.register("reports", DatasetReportViewSet, basename="report")
# Named, shareable explorations. Explorer state lived in one browser's localStorage.
router.register("saved-views", SavedViewViewSet, basename="saved-view")

urlpatterns = router.urls
