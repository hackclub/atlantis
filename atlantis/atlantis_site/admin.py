from django.contrib import admin

from .models import AuditLog, LookoutSession


@admin.register(LookoutSession)
class LookoutSessionAdmin(admin.ModelAdmin):
    list_display = ("session_id", "project", "owner", "status", "tracked_seconds", "screenshot_count", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("session_id", "owner__username", "project__title")
    # token is a secret credential — keep it out of the changelist.
    readonly_fields = ("session_id", "token", "created_at", "updated_at")
    date_hierarchy = "created_at"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target", "ip_address")
    list_filter = ("action", "created_at")
    search_fields = ("actor__username", "target", "path")
    readonly_fields = ("actor", "action", "target", "path", "method", "ip_address", "form_data", "metadata", "created_at")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
