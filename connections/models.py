from django.conf import settings
from django.db import models

from core.encryption import decrypt, encrypt
from core.models import TimeStampedModel

from .constants import SourceType


class DataSource(TimeStampedModel):
    """A registered, reusable connection to an external data source.

    Database credentials are encrypted at rest; the plaintext password is
    only ever available transiently via the ``password`` property.
    """

    class TestStatus(models.TextChoices):
        UNTESTED = "untested", "Untested"
        OK = "ok", "Connected"
        FAILED = "failed", "Failed"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        SHARED = "shared", "Shared"

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    source_type = models.CharField(max_length=20, choices=SourceType.CHOICES)

    # Connection coordinates (database sources)
    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(null=True, blank=True)
    database_name = models.CharField(max_length=255, blank=True)
    schema_name = models.CharField(max_length=128, blank=True)
    username = models.CharField(max_length=255, blank=True)
    password_encrypted = models.TextField(blank=True, default="")

    # Free-form per-type options: driver, windows_auth, extra_params, dsn,
    # service_name/sid (Oracle), path (SQLite), materialized_path + tables
    # (Excel/CSV), original_filename, etc.
    options = models.JSONField(default=dict, blank=True)

    # Ownership & sharing
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="data_sources"
    )
    site = models.ForeignKey(
        "accounts.Site", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="data_sources",
    )
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_data_sources"
    )

    # Connection-test state
    test_status = models.CharField(
        max_length=10, choices=TestStatus.choices, default=TestStatus.UNTESTED
    )
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_error = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.source_type})"

    @property
    def password(self) -> str:
        return decrypt(self.password_encrypted)

    @password.setter
    def password(self, raw: str):
        self.password_encrypted = encrypt(raw or "")

    @property
    def is_file_source(self) -> bool:
        return self.source_type in SourceType.FILE_TYPES

    def accessible_by(self, user) -> bool:
        if user.is_admin or self.owner_id == user.id:
            return True
        return self.shared_with.filter(pk=user.pk).exists()
