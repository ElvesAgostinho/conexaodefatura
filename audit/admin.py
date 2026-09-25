from django.contrib import admin

from .models import AuditEvent, IntegrationLog


class ReadOnlyAdmin(admin.ModelAdmin):
    """Registos de auditoria não podem ser criados, alterados nem apagados pelo admin."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IntegrationLog)
class IntegrationLogAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "company", "action", "status", "invoice", "simulated")
    list_filter = ("action", "status", "company", "simulated")


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "company", "actor", "action", "object_type", "object_id")
    list_filter = ("action", "company")
