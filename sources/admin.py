from django.contrib import admin

from .models import DataSource


@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "company", "kind", "system", "active", "last_sync_at", "last_sync_status")
    list_filter = ("kind", "active", "company")
    search_fields = ("code", "name", "system")
    readonly_fields = ("sync_cursor", "last_sync_at", "last_sync_status", "last_sync_message", "sync_locked_until")
