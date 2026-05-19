from django.contrib import admin

from .models import QueryDefinition


@admin.register(QueryDefinition)
class QueryDefinitionAdmin(admin.ModelAdmin):
    list_display = (
        "name", "datasource", "mode", "owner", "last_run_at", "last_row_count",
    )
    list_filter = ("mode", "visibility")
    search_fields = ("name", "description")
    readonly_fields = ("generated_sql", "last_run_at", "last_row_count")
    filter_horizontal = ("shared_with",)
