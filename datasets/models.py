from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class Dataset(TimeStampedModel):
    """A named, reusable result set backed by a saved query.

    The query result is cached on the dataset so dashboards load fast; a
    refresh re-runs the underlying query and updates the cache.
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        SHARED = "shared", "Shared"

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

    # Cached result of the last refresh
    cached_columns = models.JSONField(default=list, blank=True)
    cached_rows = models.JSONField(default=list, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    # User-defined calculated fields: [{"name": ..., "expression": ...}]
    calculated_fields = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    def accessible_by(self, user) -> bool:
        if user.is_admin or self.owner_id == user.id:
            return True
        return self.shared_with.filter(pk=user.pk).exists()
