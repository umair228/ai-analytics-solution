"""Replication bookkeeping — one row per (source, target, table).

Tracks the incremental watermark and the last full/incremental run so the
scheduler can decide what's due, and so a stale-replica alert can fire when a
table hasn't refreshed within its cadence (the linchpin of the freshness
guarantee — see DIS_TalkToData_Plan.md §5).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


class ReplicationState(TimeStampedModel):
    """Per-table mirror state from a source DataSource into a target replica."""

    class Cadence(models.TextChoices):
        HOURLY = "hourly", "Hourly (incremental)"
        DAILY = "daily", "Daily (incremental)"
        WEEKLY = "weekly", "Weekly (full)"

    source = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE,
        related_name="replication_sources")
    target = models.ForeignKey(
        "connections.DataSource", on_delete=models.CASCADE,
        related_name="replication_targets")

    table_name = models.CharField(max_length=128)
    key_columns = models.JSONField(default=list, blank=True)
    # In-place modification timestamp used for incremental pulls (CHANGED_ON on
    # the EGPC transactional tables). Blank => full-only (reference tables).
    watermark_column = models.CharField(max_length=128, blank=True)
    # Highest watermark value mirrored so far, stored as an ISO string so it works
    # for any column type and survives serialization.
    high_watermark = models.CharField(max_length=64, blank=True)

    cadence = models.CharField(max_length=10, choices=Cadence.choices,
                               default=Cadence.HOURLY)

    last_full_at = models.DateTimeField(null=True, blank=True)
    last_incremental_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    row_count = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["table_name"]
        unique_together = (("source", "target", "table_name"),)

    def __str__(self):
        return f"{self.table_name} → {self.target_id} ({self.cadence})"

    @property
    def last_synced_at(self):
        ts = [t for t in (self.last_incremental_at, self.last_full_at) if t]
        return max(ts) if ts else None

    def is_stale(self, now=None, factor=2.0) -> bool:
        """True if the table hasn't synced within ``factor``× its cadence window
        (or has a standing error / has never synced)."""
        if self.last_error:
            return True
        synced = self.last_synced_at
        if synced is None:
            return True
        window = {
            self.Cadence.HOURLY: 3600,
            self.Cadence.DAILY: 86400,
            self.Cadence.WEEKLY: 604800,
        }.get(self.cadence, 3600)
        age = (now or timezone.now()) - synced
        return age.total_seconds() > window * float(factor)
