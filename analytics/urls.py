from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AnalysisRunViewSet,
    anomaly_detect,
    associations,
    cluster,
    dataset_links,
    discover_relationships,
    explain_view,
    forecast_advanced,
    forecast_metric,
    root_cause,
    stats_catalog,
    stats_run,
)

router = DefaultRouter()
router.register("analytics/runs", AnalysisRunViewSet, basename="analysis-run")

urlpatterns = [
    path("analytics/stats/catalog/", stats_catalog, name="stats-catalog"),
    path("analytics/stats/run/", stats_run, name="stats-run"),
    path("analytics/anomaly/", anomaly_detect, name="analytics-anomaly"),
    path("analytics/root-cause/", root_cause, name="analytics-root-cause"),
    path("analytics/cluster/", cluster, name="analytics-cluster"),
    path("analytics/associations/", associations, name="analytics-associations"),
    path("analytics/forecast-metric/", forecast_metric, name="analytics-forecast-metric"),
    path("analytics/forecast/", forecast_advanced, name="analytics-forecast"),
    path("analytics/relationships/", discover_relationships, name="analytics-relationships"),
    path("analytics/dataset-links/", dataset_links, name="analytics-dataset-links"),
    path("analytics/explain/", explain_view, name="analytics-explain"),
] + router.urls
