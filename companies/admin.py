from django.contrib import admin, messages

from .models import ApiKey, Company, Membership


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "nif", "active")
    search_fields = ("name", "nif")
    list_filter = ("active",)
    inlines = [MembershipInline]


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "source", "prefix", "active", "created_at", "last_used_at")
    list_filter = ("active", "company")
    readonly_fields = ("prefix", "created_at", "last_used_at", "revoked_at")
    fields = ("company", "source", "name", "active", "prefix", "created_at", "last_used_at", "revoked_at")

    def get_readonly_fields(self, request, obj=None):
        # A empresa e a origem de uma chave existente não mudam.
        return self.readonly_fields + (("company", "source") if obj else ())

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            return
        # Criação pelo admin: gerar a chave e mostrá-la uma única vez.
        try:
            api_key, raw_key = ApiKey.generate(obj.company, obj.name, obj.source)
        except ValueError as exc:
            messages.error(request, str(exc))
            raise
        obj.pk, obj.prefix, obj.key_hash = api_key.pk, api_key.prefix, api_key.key_hash
        obj._state.adding = False
        messages.warning(request, f"Chave criada. Copie agora, não voltará a ser mostrada: {raw_key}")
