from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "target_type", "target_id", "summary")
    list_filter = ("action", "target_type", "created_at")
    search_fields = ("summary", "target_id", "user__username", "reason")
    readonly_fields = (
        "user", "action", "target_type", "target_id",
        "summary", "detail", "reason", "ip_address", "created_at",
    )

    # Append-only: the trail can be read, never written or erased. Without the
    # delete override a staff user could bulk-remove the very records that prove
    # what they did — which is the one thing an audit trail exists to prevent.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
