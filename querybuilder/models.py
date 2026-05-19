from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class QueryDefinition(TimeStampedModel):
    """A saved query — either a visual builder spec or hand-written SQL."""

    class Mode(models.TextChoices):
        BUILDER = "builder", "Visual Builder"
        RAW = "raw", "Raw SQL"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        SHARED = "shared", "Shared"

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    datasource = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE, related_name="queries"
    )
    database = models.CharField(
        max_length=255, blank=True,
        help_text="Optional target database override.",
    )
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.BUILDER)
    spec = models.JSONField(default=dict, blank=True)
    raw_sql = models.TextField(blank=True)
    generated_sql = models.TextField(blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="queries"
    )
    site = models.ForeignKey(
        "accounts.Site", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="queries",
    )
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_queries"
    )

    # Declared parameters: [{"name": "start_date", "type": "date", "default": "", "required": true}, ...]
    parameters = models.JSONField(default=list, blank=True)

    last_run_at = models.DateTimeField(null=True, blank=True)
    last_row_count = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name


class QueryRun(TimeStampedModel):
    """A single execution of a query — saved or ad-hoc."""

    query = models.ForeignKey(
        QueryDefinition, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="runs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="query_runs",
    )
    datasource = models.ForeignKey(
        "connections.DataSource", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    sql = models.TextField(blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        label = self.query.name if self.query_id else "ad-hoc"
        return f"{label} by {self.user_id} @ {self.created_at}"
