"""Multiempresa: cada empresa só vê os seus próprios documentos."""

import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

validate_host_connection = RegexValidator(
    r"^[A-Za-z][A-Za-z0-9_]{0,49}$",
    "Use só letras, números e _, a começar por letra (ex.: default, HOTEL2).",
)


class Company(models.Model):
    name = models.CharField("nome", max_length=200)
    nif = models.CharField("NIF", max_length=20, unique=True)
    active = models.BooleanField("ativa", default=True)
    # As origens de documentos (HOST, outros sistemas, API) estão em sources.DataSource
    # e a configuração AGT em agt.AgtConfiguration.
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "empresa"
        verbose_name_plural = "empresas"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.nif})"


class Membership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Administrador"
        OPERATOR = "OPERATOR", "Operador"
        VIEWER = "VIEWER", "Consulta"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField("papel", max_length=10, choices=Role.choices, default=Role.VIEWER)

    class Meta:
        verbose_name = "acesso de utilizador"
        verbose_name_plural = "acessos de utilizadores"
        constraints = [models.UniqueConstraint(fields=["user", "company"], name="uniq_membership")]

    def __str__(self):
        return f"{self.user} → {self.company} ({self.get_role_display()})"


API_KEY_PREFIX = "gw"


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class ApiKey(models.Model):
    """Chave de API de uma empresa, ligada a uma origem do tipo API.

    Só o hash é guardado; a chave é mostrada uma única vez.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="api_keys")
    source = models.ForeignKey("sources.DataSource", on_delete=models.CASCADE, related_name="api_keys",
                               verbose_name="origem")
    name = models.CharField("nome", max_length=100)
    prefix = models.CharField(max_length=16, unique=True, editable=False)
    key_hash = models.CharField(max_length=64, editable=False)
    active = models.BooleanField("ativa", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "chave de API"
        verbose_name_plural = "chaves de API"

    def __str__(self):
        return f"{self.name} ({API_KEY_PREFIX}_{self.prefix}_…)"

    @classmethod
    def generate(cls, company: Company, name: str, source=None) -> tuple["ApiKey", str]:
        """Sem `source`, usa (ou cria) a origem API por omissão da empresa."""
        from sources.models import DataSource

        if source is None:
            source = DataSource.default_api_source(company)
        if source.company_id != company.pk or source.kind != DataSource.Kind.API:
            raise ValueError("A chave tem de ficar ligada a uma origem do tipo API da mesma empresa.")
        prefix = secrets.token_hex(4)
        raw_key = f"{API_KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
        api_key = cls.objects.create(company=company, source=source, name=name, prefix=prefix,
                                     key_hash=_hash_key(raw_key))
        return api_key, raw_key

    @classmethod
    def authenticate(cls, raw_key: str) -> "ApiKey | None":
        parts = (raw_key or "").strip().split("_", 2)
        if len(parts) != 3 or parts[0] != API_KEY_PREFIX:
            return None
        api_key = cls.objects.select_related("company", "source").filter(prefix=parts[1]).first()
        if (
            api_key is None
            or not api_key.active
            or not api_key.company.active
            or not api_key.source.active
            or not hmac.compare_digest(api_key.key_hash, _hash_key(raw_key.strip()))
        ):
            return None
        cls.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        return api_key

    def revoke(self) -> None:
        self.active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["active", "revoked_at"])
