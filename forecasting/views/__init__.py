from .downtime import DowntimeForecastView
from .sample import SampleForecast, SectionForecast
from .inventory import InventoryForecast
from .insights import ForecastInsightsView

__all__ = [
    "DowntimeForecastView",
    "SampleForecast",
    "SectionForecast",
    "InventoryForecast",
    "ForecastInsightsView",
]
