from django.contrib import admin

from .models import AgtConfiguration


@admin.register(AgtConfiguration)
class AgtConfigurationAdmin(admin.ModelAdmin):
    list_display = ("company", "environment", "auto_send", "updated_at")
    list_filter = ("environment", "auto_send")
    readonly_fields = ("updated_at", "updated_by")
