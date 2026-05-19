from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import DataSourceViewSet, source_types

router = DefaultRouter()
router.register("datasources", DataSourceViewSet, basename="datasource")

urlpatterns = [
    path("connections/source-types/", source_types, name="source-types"),
] + router.urls
