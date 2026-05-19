from rest_framework.routers import DefaultRouter

from .views import APITokenViewSet, LabViewSet, OrganizationViewSet, SiteViewSet, UserViewSet

router = DefaultRouter()
router.register("users", UserViewSet, basename="user")
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("sites", SiteViewSet, basename="site")
router.register("labs", LabViewSet, basename="lab")
router.register("api-tokens", APITokenViewSet, basename="api-token")

urlpatterns = router.urls
