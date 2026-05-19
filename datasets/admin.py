from django.contrib import admin

from .models import Dataset


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ("name", "query", "owner", "row_count", "last_refreshed_at")
    list_filter = ("visibility",)
    search_fields = ("name", "description")
    readonly_fields = (
        "cached_columns", "cached_rows", "row_count",
        "last_refreshed_at", "last_error",
    )
    filter_horizontal = ("shared_with",)
