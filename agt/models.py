"""Configuração da comunicação com a AGT, por empresa.

O Gateway é apenas o canal de comunicação: não emite faturas nem as assina. As credenciais
da AGT são do CONTRIBUINTE (a empresa cliente), que as obtém junto da AGT e as fornece; os
dados de certificação são os do software de faturação do cliente (ex.: HOST), que emite
as faturas.

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
        "endereço dos serviços AGT", blank=True,
        help_text="Vazio = o endereço oficial do ambiente escolhido (homologação ou produção).",
    )
    # --- Acesso à API (Basic Authentication; credenciais atribuídas pela AGT ao produtor de software)
    api_username = models.CharField("utilizador da API", max_length=200, blank=True)
    api_password_encrypted = models.TextField(blank=True, editable=False)
    # --- Chave do contribuinte (emissor): assina cada fatura (jwsDocumentSignature)
    issuer_key_encrypted = models.TextField(blank=True, editable=False)
    issuer_key_info = models.CharField(max_length=120, blank=True, editable=False)
    # --- Software de faturação (softwareInfo): jwsSoftwareSignature feita com a chave do produtor
    signature_version = models.PositiveIntegerField(
        "versão da assinatura do software (signatureVersion)", null=True, blank=True)
    software_signature = models.TextField(
        "assinatura do software (jwsSoftwareSignature)", blank=True,
        help_text="Fornecida pelo produtor do software de faturação. Em alternativa, carregue a chave do produtor.")
    producer_key_encrypted = models.TextField(blank=True, editable=False)
    producer_key_info = models.CharField(max_length=120, blank=True, editable=False)
    # Dados do software que EMITE as faturas (o sistema de faturação do cliente), se a AGT os pedir.
    software_certificate_number = models.CharField(
        "n.º de certificação do software (softwareValidationNumber)", max_length=50, blank=True,
        help_text="Certificado AGT do programa que emite as faturas (ex.: HOST). Fornecido pelo cliente ou "
                  "pelo fornecedor desse programa. O Gateway não emite faturas.")
    software_name = models.CharField("nome do software (productId)", max_length=100, blank=True)
    software_version = models.CharField("versão do software (productVersion)", max_length=30, blank=True)
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

    # URLs oficiais (documentação da AGT, Quiosque AGT › Facturação Electrónica).
    OFFICIAL_URLS = {
        Environment.TEST: "https://sifphml.minfin.gov.ao/sigt/fe/v1/",
        Environment.PRODUCTION: "https://sifp.minfin.gov.ao/sigt/fe/v1/",
    }

    @property
    def effective_base_url(self) -> str:
        return self.base_url or self.OFFICIAL_URLS.get(self.environment, "")

    # ------------------------------------------------ credenciais (cifradas)

    def set_api_password(self, raw: str) -> None:
        from config.crypto import encrypt

        self.api_password_encrypted = encrypt(raw)

    def api_password(self) -> str:
        from config.crypto import decrypt

        return decrypt(self.api_password_encrypted)

    def set_key(self, kind: str, loaded) -> None:
        from config.crypto import encrypt

        setattr(self, f"{kind}_key_encrypted", encrypt(loaded.pem))
        setattr(self, f"{kind}_key_info", loaded.info)

    def clear_key(self, kind: str) -> None:
        setattr(self, f"{kind}_key_encrypted", "")
        setattr(self, f"{kind}_key_info", "")

    def private_key(self, kind: str):
        """Chave privada RSA carregada no painel ('issuer' ou 'producer'), ou None."""
        from config.crypto import decrypt

        from .keys import private_key_from_pem

        token = getattr(self, f"{kind}_key_encrypted")
        return private_key_from_pem(decrypt(token)) if token else None

    def credentials_status(self) -> list[dict]:
        """O que está configurado, sem revelar valores (para o painel)."""
        return [
            {"label": "Utilizador e senha da API", "owner": "produtor do software (atribuídos pela AGT)",
             "ok": bool(self.api_username and self.api_password_encrypted),
             "detail": self.api_username or ""},
            {"label": "Chave privada do contribuinte", "owner": "cliente (gerada pela AGT, portal do contribuinte)",
             "ok": bool(self.issuer_key_encrypted), "detail": self.issuer_key_info},
            {"label": "Assinatura do software", "owner": "produtor do software de faturação",
             "ok": bool(self.software_signature or self.producer_key_encrypted),
             "detail": self.producer_key_info or ("assinatura fornecida" if self.software_signature else "")},
        ]

    def clean(self):
        errors = {}
        if self.base_url and not self.base_url.lower().startswith("https://"):
            errors["base_url"] = "Tem de usar https://."

        if errors:
            raise ValidationError(errors)

    def missing_for_real_sending(self) -> list[str]:
        """O que falta para poder comunicar de verdade (mostrado no painel)."""
        missing = []
        if not self.effective_base_url:
            missing.append("endereço dos serviços AGT")
        if not (self.api_username and self.api_password_encrypted):
            missing.append("utilizador e senha da API")
        if not self.issuer_key_encrypted:
            missing.append("chave privada do contribuinte")
        if not (self.software_name and self.software_version and self.software_certificate_number
                and self.signature_version):
            missing.append("dados do software de faturação (nome, versão, n.º de certificação, versão da assinatura)")
        if not (self.software_signature or self.producer_key_encrypted):
            missing.append("assinatura do software (ou chave do produtor)")
        return missing
