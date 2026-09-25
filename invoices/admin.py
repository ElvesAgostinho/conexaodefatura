from django.contrib import admin

from .models import Invoice, InvoiceItem


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    can_delete = False

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    """Só consulta: os documentos vêm das origens e não são editados à mão."""

    list_display = ("document_number", "company", "source_system", "document_date", "total", "status")
    list_filter = ("status", "company", "source_system")
    search_fields = ("document_number", "customer_name", "customer_nif", "source_document_id")
    inlines = [InvoiceItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
