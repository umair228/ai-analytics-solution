from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Lab, Organization, Site, User


@admin.register(User)
class DSEUserAdmin(UserAdmin):
    list_display = ("username", "email", "role", "organization", "is_active")
    list_filter = ("role", "is_active", "organization")
    fieldsets = UserAdmin.fieldsets + (
        (
            "DSE profile",
            {"fields": ("role", "organization", "sites", "labs", "job_title", "phone")},
        ),
    )
    filter_horizontal = UserAdmin.filter_horizontal + ("sites", "labs")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "created_at")
    search_fields = ("name", "code")


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "code", "is_active")
    list_filter = ("organization", "is_active")
    search_fields = ("name", "code")


@admin.register(Lab)
class LabAdmin(admin.ModelAdmin):
    list_display = ("name", "site", "code", "is_active")
    list_filter = ("site", "is_active")
    search_fields = ("name", "code")
