from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "target_type", "target_id", "summary")
    list_filter = ("action", "target_type", "created_at")
    search_fields = ("summary", "target_id", "user__username")
    readonly_fields = (
        "user", "action", "target_type", "target_id",
        "summary", "detail", "ip_address", "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
