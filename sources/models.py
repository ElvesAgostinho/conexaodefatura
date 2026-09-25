"""Origens de documentos de cada empresa.

Uma empresa pode ter várias origens, de sistemas diferentes (HOST Hotel Systems, outro
ERP, POS, ...). Dois tipos:

  DATABASE  o Gateway lê (só leitura) uma BD de qualquer SGBD, com consultas e
            mapeamento de colunas configurados em `mapping` (ver sources.mapping).
  API       o próprio sistema envia os documentos para a API do Gateway, autenticado
            com uma chave de API ligada a esta origem.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from companies.models import Company, validate_host_connection

validate_source_code = RegexValidator(
    r"^[A-Z0-9][A-Z0-9_\-]{0,29}$", "Maiúsculas, números, _ ou -, até 30 (ex.: HOST, ERP-LOJA1)."
)

SYNC_LOCK = timedelta(minutes=15)


class DataSource(models.Model):
    class Kind(models.TextChoices):
        DATABASE = "DATABASE", "Base de dados (leitura)"
        API = "API", "API (o sistema envia)"

    class SyncStatus(models.TextChoices):
        NEVER = "", "Nunca"
        OK = "OK", "OK"
        PARTIAL = "PARTIAL", "Parcial"
        ERROR = "ERROR", "Erro"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sources")
    code = models.CharField(
        "código", max_length=30, validators=[validate_source_code],
        help_text="Identifica a origem nos documentos (sistema de origem). Não alterar depois de importar.",
    )
    name = models.CharField("nome", max_length=100)
    system = models.CharField("sistema", max_length=100, blank=True,
                              help_text="Sistema de faturação de origem (ex.: HOST Hotel Systems).")
    kind = models.CharField("tipo", max_length=10, choices=Kind.choices)
    active = models.BooleanField("ativa", default=True)

    # Só DATABASE. As credenciais ficam no .env (HOST_DB_* ou HOST_<NOME>_DB_*).
    connection = models.CharField(
        "ligação", max_length=50, blank=True, validators=[validate_host_connection],
        help_text="'default' usa HOST_DB_*; outro nome X usa HOST_X_DB_* no .env.",
    )
    mapping = models.JSONField("mapeamento", default=dict, blank=True)

    sync_cursor = models.CharField("cursor", max_length=200, blank=True, editable=False)
    sync_locked_until = models.DateTimeField(null=True, blank=True, editable=False)
    last_sync_at = models.DateTimeField("última sincronização", null=True, blank=True, editable=False)
    last_sync_status = models.CharField(max_length=10, choices=SyncStatus.choices, blank=True, editable=False)
    last_sync_message = models.TextField(blank=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "origem de documentos"
        verbose_name_plural = "origens de documentos"
        ordering = ["company__name", "code"]
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_source_code")]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def clean(self):
        errors = {}
        if self.kind == self.Kind.DATABASE:
            if not self.connection:
                errors["connection"] = "Obrigatória para origens do tipo base de dados."
            if self.mapping:
                from .mapping import validate_mapping

                problems = validate_mapping(self.mapping)
                if problems:
                    errors["mapping"] = problems
        elif self.kind == self.Kind.API:
            if self.connection:
                errors["connection"] = "Origens API não usam ligação à base de dados."
            if self.mapping:
                errors["mapping"] = "Origens API não usam mapeamento: enviam já no formato canónico."
        if errors:
            raise ValidationError(errors)

    # ------------------------------------------------------------ sincronização

    def acquire_sync_lock(self) -> bool:
        """Impede duas sincronizações simultâneas da mesma origem (UPDATE condicional, portável)."""
        now = timezone.now()
        updated = DataSource.objects.filter(pk=self.pk).filter(
            models.Q(sync_locked_until__isnull=True) | models.Q(sync_locked_until__lt=now)
        ).update(sync_locked_until=now + SYNC_LOCK)
        return updated == 1

    def release_sync_lock(self) -> None:
        DataSource.objects.filter(pk=self.pk).update(sync_locked_until=None)

    @classmethod
    def default_api_source(cls, company: Company) -> "DataSource":
        source, _ = cls.objects.get_or_create(
            company=company, code="API", defaults={"name": "API", "kind": cls.Kind.API}
        )
        if source.kind != cls.Kind.API:
            raise ValidationError("A origem com código API desta empresa não é do tipo API.")
        return source
