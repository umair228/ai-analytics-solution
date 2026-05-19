from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


class Dataset(TimeStampedModel):
    """A named, reusable result set backed by a saved query."""

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        SHARED = "shared", "Shared"

    class RefreshInterval(models.TextChoices):
        MANUAL = "manual", "Manual only"
        HOURLY = "hourly", "Every hour"
        DAILY = "daily", "Every day"
        WEEKLY = "weekly", "Every week"

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    query = models.ForeignKey(
        "querybuilder.QueryDefinition",
        on_delete=models.CASCADE,
        related_name="datasets",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="datasets"
    )
    site = models.ForeignKey(
        "accounts.Site", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="datasets",
    )
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_datasets"
    )

    cached_columns = models.JSONField(default=list, blank=True)
    cached_rows = models.JSONField(default=list, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    calculated_fields = models.JSONField(default=list, blank=True)

    refresh_interval = models.CharField(
        max_length=10, choices=RefreshInterval.choices,
        default=RefreshInterval.MANUAL,
    )
    next_refresh_at = models.DateTimeField(null=True, blank=True)

    # Default values for any query parameters defined on the underlying query.
    # Format: {"param_name": "value", ...}
    param_defaults = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    INTERVAL_DELTA = {
        RefreshInterval.HOURLY: timedelta(hours=1),
        RefreshInterval.DAILY: timedelta(days=1),
        RefreshInterval.WEEKLY: timedelta(weeks=1),
    }

    def schedule_next_refresh(self, from_time=None):
        delta = self.INTERVAL_DELTA.get(self.refresh_interval)
        self.next_refresh_at = (from_time or timezone.now()) + delta if delta else None
        return self.next_refresh_at

    def accessible_by(self, user) -> bool:
        if user.is_admin or self.owner_id == user.id:
            return True
        return self.shared_with.filter(pk=user.pk).exists()


class DatasetAlert(TimeStampedModel):
    """A threshold alert on a dataset column — fires an event on refresh."""

    class Condition(models.TextChoices):
        GT  = "gt",  "Greater than"
        GTE = "gte", "Greater than or equal"
        LT  = "lt",  "Less than"
        LTE = "lte", "Less than or equal"
        EQ  = "eq",  "Equal to"

    class Aggregation(models.TextChoices):
        AVG   = "avg",   "Average"
        SUM   = "sum",   "Sum"
        COUNT = "count", "Count"
        MAX   = "max",   "Maximum"
        MIN   = "min",   "Minimum"
        LAST  = "last",  "Last value"

    name        = models.CharField(max_length=200)
    dataset     = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="alerts")
    column      = models.CharField(max_length=200)
    aggregation = models.CharField(
        max_length=10, choices=Aggregation.choices, default=Aggregation.AVG
    )
    condition   = models.CharField(max_length=5, choices=Condition.choices)
    threshold   = models.FloatField()
    is_active   = models.BooleanField(default=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alerts"
    )

    last_checked_at   = models.DateTimeField(null=True, blank=True)
    last_triggered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AlertEvent(TimeStampedModel):
    """A single firing of a DatasetAlert — created when the condition is met."""

    alert           = models.ForeignKey(DatasetAlert, on_delete=models.CASCADE, related_name="events")
    triggered_value = models.FloatField()
    message         = models.TextField()
    acknowledged    = models.BooleanField(default=False)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="acknowledged_events"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Event for '{self.alert.name}' @ {self.created_at:%Y-%m-%d %H:%M}"
