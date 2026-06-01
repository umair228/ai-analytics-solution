"""URL routes for the forecasting app (mounted under /api/)."""
from django.urls import path

from .views import (
    DowntimeForecastView,
    SampleForecast,
    SectionForecast,
    InventoryForecast,
)

urlpatterns = [
    path("downtime-forecast/", DowntimeForecastView.as_view(), name="downtime-forecast"),
    path("sample-forecast/", SampleForecast.as_view(), name="sample-forecast"),
    path("section-forecast/", SectionForecast.as_view(), name="section-forecast"),
    path("inventory-forecast/", InventoryForecast.as_view(), name="inventory-forecast"),
]
