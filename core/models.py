from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base adding created/updated timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """Immutable audit trail of meaningful actions across the platform."""

    class Action(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"
        TEST = "TEST", "Test connection"
        INTROSPECT = "INTROSPECT", "Inspect schema"
        QUERY = "QUERY", "Execute query"
        LOGIN = "LOGIN", "Login"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    summary = models.CharField(max_length=255, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.target_type}#{self.target_id} by {self.user_id}"
