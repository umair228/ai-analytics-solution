from django.contrib import admin

from .models import DataSource


@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    list_display = (
        "name", "source_type", "host", "database_name", "owner", "test_status",
    )
    list_filter = ("source_type", "test_status", "visibility")
    search_fields = ("name", "host", "database_name")
    readonly_fields = (
        "test_status", "last_tested_at", "last_test_error",
        "password_encrypted", "created_at", "updated_at",
    )
    filter_horizontal = ("shared_with",)
