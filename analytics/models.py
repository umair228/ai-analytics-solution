"""Persistence for the analytics workflows.

``AnalysisRun`` is a single, workflow-discriminated record that backs the
"saved run history" feature for the Anomaly Detection, Statistical Calculations
and Forecasting standalone workflows. Every run shares the same shape — a
dataset + the request ``config`` + the engine ``result`` + a small ``metrics``
projection for cheap list rendering — so one table and one API serve all three.

Runs are written synchronously today; the ``PENDING``/``RUNNING`` statuses and
``duration_ms`` field are the seam for a future async (Celery/django-q) runner.
"""
from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class AnalysisRun(TimeStampedModel):
    """A saved execution of an analytics workflow on a dataset."""

    class Workflow(models.TextChoices):
        ANOMALY = "anomaly", "Anomaly detection"
        STATISTICS = "statistics", "Statistical test"
        FORECAST = "forecast", "Forecast"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"   # reserved for future async execution
        RUNNING = "running", "Running"   # reserved for future async execution
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=200, blank=True)
    workflow = models.CharField(max_length=20, choices=Workflow.choices)
    method = models.CharField(max_length=64, blank=True)

    dataset = models.ForeignKey(
        "datasets.Dataset", on_delete=models.CASCADE, related_name="analysis_runs"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="analysis_runs"
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_analysis_runs"
    )

    config = models.JSONField(default=dict, blank=True)    # the request body
    result = models.JSONField(default=dict, blank=True)    # the engine's result dict
    metrics = models.JSONField(default=dict, blank=True)   # small projection for lists
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SUCCESS
    )
    error = models.TextField(blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "workflow", "-created_at"]),
            models.Index(fields=["dataset", "workflow"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.workflow}:{self.method} #{self.pk}"

    def accessible_by(self, user) -> bool:
        """Mirror ``Dataset.accessible_by`` — admin, owner, or explicitly shared."""
        if getattr(user, "is_admin", False) or self.owner_id == user.id:
            return True
        return self.shared_with.filter(pk=user.pk).exists()
