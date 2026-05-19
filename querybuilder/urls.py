from rest_framework.routers import DefaultRouter

from .views import QueryDefinitionViewSet

router = DefaultRouter()
router.register("queries", QueryDefinitionViewSet, basename="query")

urlpatterns = router.urls
