from django.contrib import admin

from .models import Dashboard, Widget


class WidgetInline(admin.TabularInline):
    model = Widget
    extra = 0


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "visibility", "updated_at")
    list_filter = ("visibility",)
    search_fields = ("name", "description")
    filter_horizontal = ("shared_with",)
    inlines = [WidgetInline]


@admin.register(Widget)
class WidgetAdmin(admin.ModelAdmin):
    list_display = ("title", "dashboard", "widget_type", "dataset")
    list_filter = ("widget_type",)
