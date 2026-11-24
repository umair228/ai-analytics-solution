from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base adding created/updated timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """Append-only audit trail of meaningful actions across the platform.

    Append-only is enforced at the application layer: the API exposes it through
    a ReadOnlyModelViewSet, and ``core.admin.AuditLogAdmin`` denies add/change/
    delete. Writes go through ``core.audit.log_audit``, which fails closed.

    And at the DATABASE layer, once ``db/protect-audit-trail-postgres.sql`` has been applied
    (``make protect-audit``): an append-only trigger refuses UPDATE and DELETE on this table
    outright. That is not the same as a REVOKE and the difference matters — DIS connects to
    its own metadata database as a Postgres *superuser*, and a superuser bypasses privilege
    checks entirely, so a REVOKE protects nothing from it. A trigger fires whatever the
    privileges say.

    The limit, stated honestly: a superuser can still disable the trigger. No database
    defends against its own superuser. What the trigger changes is that tampering with the
    trail is no longer an UPDATE that leaves no mark — it is the deliberate dismantling of a
    named protection, which is the difference between an accident and a decision.
    """

    class Action(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"
        TEST = "TEST", "Test connection"
        INTROSPECT = "INTROSPECT", "Inspect schema"
        QUERY = "QUERY", "Execute query"
        LOGIN = "LOGIN", "Login"
        # Part 11 §11.300(d) — "detect and report in an immediate and urgent manner any
        # attempts at ... unauthorized use". None of these were recorded: a failed login
        # produced no entry at all, so the trail could not distinguish a quiet night from
        # a night spent guessing passwords.
        LOGIN_FAILED = "LOGIN_FAILED", "Failed login"
        LOCKOUT = "LOCKOUT", "Account locked (repeated failures)"
        UNLOCK = "UNLOCK", "Account unlocked"
        DENIED = "DENIED", "Access denied"

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
    # For an UPDATE this holds {"changes": {field: {"from": …, "to": …}}} — Part 11
    # §11.10(e): a change must not obscure what was there before. Secrets are redacted to
    # "***" by core.audit before they get here; the fact of the change is recorded, the
    # value never is.
    detail = models.JSONField(default=dict, blank=True)
    # Why. Empty unless the caller stated one (or DSE_REQUIRE_CHANGE_REASON demands it).
    reason = models.CharField(max_length=500, blank=True)
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
