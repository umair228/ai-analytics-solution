from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Lab, Organization, Site, User


@admin.register(User)
class DSEUserAdmin(UserAdmin):
    list_display = ("username", "email", "role", "organization", "is_active",
                    "mfa_on", "lock_state")
    list_filter = ("role", "is_active", "organization")
    readonly_fields = ("failed_login_count", "locked_until")
    fieldsets = UserAdmin.fieldsets + (
        (
            "DIS profile",
            {"fields": ("role", "organization", "sites", "labs", "job_title", "phone")},
        ),
        (
            "Security",
            {"fields": ("failed_login_count", "locked_until"),
             "description": "Locked by repeated failed logins (Part 11 §11.300(d)). The "
                            "lock expires on its own; use the action below to clear it "
                            "sooner. Deactivating the account is a different thing and "
                            "should not be used for this."},
        ),
    )
    filter_horizontal = UserAdmin.filter_horizontal + ("sites", "labs")
    actions = ["unlock_accounts", "reset_mfa"]

    @admin.display(description="MFA", boolean=True)
    def mfa_on(self, obj):
        from . import mfa as mfa_mod

        return mfa_mod.is_active(obj)

    @admin.action(description="Remove MFA from the selected accounts (lost phone)")
    def reset_mfa(self, request, queryset):
        """The way back when the phone is gone AND the backup codes are gone.

        It is deliberately an administrator's act, and it is audited with their name on it —
        because "I've lost my phone" is also what somebody says when they are trying to get
        into an account that is not theirs.
        """
        from core.audit import client_ip

        from . import mfa as mfa_mod

        n = 0
        for user in queryset:
            if mfa_mod.device_for(user):
                mfa_mod.disable(user, by=request.user, ip=client_ip(request))
                n += 1
        self.message_user(
            request,
            f"Removed MFA from {n} account(s). They will be prompted to set it up again on "
            f"next sign-in if their role requires it. The removal is in the audit trail, "
            f"with your name on it.")

    @admin.display(description="Login")
    def lock_state(self, obj):
        if obj.is_locked:
            return f"🔒 locked until {obj.locked_until:%H:%M}"
        if obj.failed_login_count:
            return f"{obj.failed_login_count} failed"
        return "—"

    @admin.action(description="Unlock the selected accounts (clears failed-login count)")
    def unlock_accounts(self, request, queryset):
        # A lockout that only an expiring timer can clear is a support ticket by design.
        # The unlock is itself audited — it is a security decision, and who made it is
        # part of the record.
        from . import lockout as lockout_mod
        from core.audit import client_ip

        n = 0
        for user in queryset:
            lockout_mod.unlock(user, by=request.user, ip=client_ip(request))
            n += 1
        self.message_user(request, f"Unlocked {n} account(s). The unlock is in the audit "
                                   f"trail.")


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
