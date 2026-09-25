"""Configuração da comunicação com a AGT, por empresa.

Só guarda parâmetros NÃO sensíveis. Segredos (client secret, chave privada e a sua
senha) ficam no .env: AGT_* para a configuração por omissão, ou AGT_<PREFIXO>_* quando a
empresa tem um prefixo de credenciais próprio.
"""

import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from companies.models import Company

SECRET_NAMES = ("CLIENT_SECRET", "PRIVATE_KEY", "PRIVATE_KEY_PASSWORD")

validate_credentials_prefix = RegexValidator(
    r"^[A-Za-z][A-Za-z0-9_]{0,29}$", "Letras, números e _, a começar por letra (ex.: HOTEL2)."
)


class AgtConfiguration(models.Model):
    class Environment(models.TextChoices):
        SIMULATION = "SIMULATION", "Simulação (nada é enviado à AGT)"
        TEST = "TEST", "Testes da AGT"
        PRODUCTION = "PRODUCTION", "Produção"

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="agt_config")
    environment = models.CharField("ambiente", max_length=12, choices=Environment.choices,
                                   default=Environment.SIMULATION)
    base_url = models.URLField(
        "endereço do serviço AGT", blank=True,
        help_text="Endereço oficial indicado pela AGT para o ambiente escolhido (https).",
    )
    software_certificate_number = models.CharField("n.º de certificado do software", max_length=50, blank=True)
    software_name = models.CharField("nome do software", max_length=100, blank=True)
    software_version = models.CharField("versão do software", max_length=30, blank=True)
    client_id = models.CharField("client ID", max_length=200, blank=True)
    credentials_prefix = models.CharField(
        "prefixo das credenciais", max_length=30, blank=True, validators=[validate_credentials_prefix],
        help_text="Vazio: usa AGT_CLIENT_SECRET, AGT_PRIVATE_KEY... do .env. X: usa AGT_X_CLIENT_SECRET, ...",
    )

    auto_send = models.BooleanField("enviar automaticamente", default=False,
                                    help_text="Coloca na fila de envio os documentos validados.")
    timeout_seconds = models.PositiveIntegerField(
        "tempo limite (s)", default=30, validators=[MinValueValidator(1), MaxValueValidator(300)])
    max_attempts = models.PositiveIntegerField(
        "máximo de tentativas", default=5, validators=[MinValueValidator(1), MaxValueValidator(20)])
    retry_delay_seconds = models.PositiveIntegerField(
        "espera entre tentativas (s)", default=300, validators=[MinValueValidator(10), MaxValueValidator(86400)],
        help_text="Duplica a cada tentativa falhada (máximo 24 h).",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   editable=False)

    class Meta:
        verbose_name = "configuração AGT"
        verbose_name_plural = "configurações AGT"

    def __str__(self):
        return f"AGT — {self.company} ({self.get_environment_display()})"

    @classmethod
    def for_company(cls, company: Company) -> "AgtConfiguration":
        config, _ = cls.objects.get_or_create(company=company)
        return config

    @property
    def is_simulation(self) -> bool:
        return self.environment == self.Environment.SIMULATION

    def secret_env_name(self, name: str) -> str:
        prefix = f"AGT_{self.credentials_prefix.upper()}_" if self.credentials_prefix else "AGT_"
        return f"{prefix}{name}"

    def secrets_status(self) -> dict[str, bool]:
        """Nome da variável -> definida? Nunca devolve os valores."""
        return {self.secret_env_name(name): bool(os.environ.get(self.secret_env_name(name))) for name in SECRET_NAMES}

    def get_secret(self, name: str) -> str | None:
        return os.environ.get(self.secret_env_name(name)) or None

    def clean(self):
        errors = {}
        if self.base_url and not self.base_url.lower().startswith("https://"):
            errors["base_url"] = "Tem de usar https://."
        if not self.is_simulation:
            required = {
                "base_url": self.base_url,
                "software_certificate_number": self.software_certificate_number,
            }
            for field_name, value in required.items():
                if not value:
                    errors[field_name] = "Obrigatório fora do modo simulação."
        if errors:
            raise ValidationError(errors)

    def missing_for_real_sending(self) -> list[str]:
        """O que falta para poder comunicar de verdade (mostrado no painel)."""
        missing = []
        if not self.base_url:
            missing.append("endereço do serviço AGT")
        if not self.software_certificate_number:
            missing.append("n.º de certificado do software")
        missing += [f"variável {name} no .env" for name, ok in self.secrets_status().items()
                    if not ok and not name.endswith("PRIVATE_KEY_PASSWORD")]
        return missing
