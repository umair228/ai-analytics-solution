from django.urls import path

from .views import (
    anomaly_detect,
    associations,
    cluster,
    dataset_links,
    discover_relationships,
    forecast_metric,
    root_cause,
    stats_catalog,
    stats_run,
)

urlpatterns = [
    path("analytics/stats/catalog/", stats_catalog, name="stats-catalog"),
    path("analytics/stats/run/", stats_run, name="stats-run"),
    path("analytics/anomaly/", anomaly_detect, name="analytics-anomaly"),
    path("analytics/root-cause/", root_cause, name="analytics-root-cause"),
    path("analytics/cluster/", cluster, name="analytics-cluster"),
    path("analytics/associations/", associations, name="analytics-associations"),
    path("analytics/forecast-metric/", forecast_metric, name="analytics-forecast-metric"),
    path("analytics/relationships/", discover_relationships, name="analytics-relationships"),
    path("analytics/dataset-links/", dataset_links, name="analytics-dataset-links"),
]
