"""Origens de documentos de cada empresa.

Uma empresa pode ter várias origens, de sistemas diferentes (HOST Hotel Systems, outro
ERP, POS, ...). Dois tipos:

  DATABASE  o Gateway lê (só leitura) uma BD de qualquer SGBD, com consultas e
            mapeamento de colunas configurados em `mapping` (ver sources.mapping).
            A ligação configura-se no painel (senha cifrada) ou no .env.
  API       o próprio sistema envia os documentos para a API do Gateway, autenticado
            com uma chave de API ligada a esta origem.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from companies.models import Company, validate_host_connection
from config.crypto import decrypt, encrypt

validate_source_code = RegexValidator(
    r"^[A-Z0-9][A-Z0-9_\-]{0,29}$", "Maiúsculas, números, _ ou -, até 30 (ex.: HOST, ERP-LOJA1)."
)

SYNC_LOCK = timedelta(minutes=15)


class DataSource(models.Model):
    class Kind(models.TextChoices):
        DATABASE = "DATABASE", "Base de dados (leitura)"
        API = "API", "API (o sistema envia)"

    class ConnectionMode(models.TextChoices):
        PANEL = "PANEL", "Configurada no painel"
        ENV = "ENV", "Variáveis do ficheiro .env"

    class Engine(models.TextChoices):
        MSSQL = "mssql", "SQL Server"
        POSTGRESQL = "postgresql", "PostgreSQL"
        MYSQL = "mysql", "MySQL"
        MARIADB = "mariadb", "MariaDB"
        ORACLE = "oracle", "Oracle"
        SQLITE = "sqlite", "SQLite (ficheiro)"
        URL = "url", "Outro SGBD (URL SQLAlchemy)"

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

    # Só DATABASE. Ligação configurada no painel (senha e URL cifrados) ou no .env.
    connection_mode = models.CharField("configuração da ligação", max_length=5, choices=ConnectionMode.choices,
                                       default=ConnectionMode.PANEL)
    db_engine = models.CharField("sistema de base de dados", max_length=12, choices=Engine.choices, blank=True)
    db_host = models.CharField("servidor", max_length=255, blank=True,
                               help_text="Nome ou IP. SQL Server com instância: SERVIDOR\\INSTANCIA.")
    db_port = models.PositiveIntegerField("porta", null=True, blank=True,
                                          help_text="Vazio = porta por omissão do sistema.")
    db_name = models.CharField("base de dados", max_length=255, blank=True,
                               help_text="Nome da base de dados (Oracle: serviço; SQLite: caminho do ficheiro).")
    db_user = models.CharField("utilizador", max_length=128, blank=True,
                               help_text="Utilizador só com permissão de leitura. SQL Server: vazio = autenticação Windows.")
    db_password_encrypted = models.TextField(blank=True, editable=False)
    db_url_encrypted = models.TextField(blank=True, editable=False)
    db_odbc_driver = models.CharField("driver ODBC", max_length=100, blank=True,
                                      help_text="Só SQL Server. Vazio = ODBC Driver 18 for SQL Server.")
    db_trust_server_certificate = models.BooleanField(
        "confiar no certificado do servidor", default=False,
        help_text="Só SQL Server com certificado próprio (autoassinado).")
    db_options = models.CharField("opções adicionais", max_length=500, blank=True,
                                  help_text="Formato chave=valor;chave=valor.")
    connection = models.CharField(
        "nome da ligação no .env", max_length=50, blank=True, validators=[validate_host_connection],
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

    # ------------------------------------------------------------------ ligação

    def set_db_password(self, raw: str) -> None:
        self.db_password_encrypted = encrypt(raw)

    def set_db_url(self, raw: str) -> None:
        self.db_url_encrypted = encrypt(raw)

    @property
    def has_db_password(self) -> bool:
        return bool(self.db_password_encrypted)

    def connection_config(self):
        """HostConnectionConfig desta origem. Lança HostConfigError se estiver incompleta."""
        from host_connector.config import HostConfigError, HostConnectionConfig

        if self.kind != self.Kind.DATABASE:
            raise HostConfigError("Só origens do tipo base de dados têm ligação.")
        if self.connection_mode == self.ConnectionMode.ENV:
            return HostConnectionConfig.from_env(self.connection)
        if not self.db_engine:
            raise HostConfigError("Falta escolher o sistema de base de dados.")
        try:
            password = decrypt(self.db_password_encrypted)
            url = decrypt(self.db_url_encrypted) if self.db_engine == self.Engine.URL else ""
        except ValueError as exc:
            raise HostConfigError(str(exc)) from exc
        if self.db_engine == self.Engine.URL and not url:
            raise HostConfigError("Falta indicar o URL de ligação.")
        return HostConnectionConfig.from_values(
            engine=self.db_engine, host=self.db_host, port=self.db_port, name=self.db_name, user=self.db_user,
            password=password, odbc_driver=self.db_odbc_driver,
            trust_server_certificate=self.db_trust_server_certificate, options=self.db_options, url=url,
        )

    def connection_summary(self) -> str:
        if self.kind != self.Kind.DATABASE:
            return ""
        if self.connection_mode == self.ConnectionMode.ENV:
            return f".env: {self.connection or '—'}"
        if self.db_engine == self.Engine.URL:
            return "URL SQLAlchemy"
        if self.db_engine == self.Engine.SQLITE:
            return f"SQLite · {self.db_name}"
        where = self.db_host + (f":{self.db_port}" if self.db_port else "")
        return f"{self.get_db_engine_display()} · {where} · {self.db_name}" if self.db_engine else "por configurar"

    def clean(self):
        errors = {}
        if self.kind == self.Kind.DATABASE:
            if self.connection_mode == self.ConnectionMode.ENV and not self.connection:
                errors["connection"] = "Indique o nome da ligação definida no .env (ex.: default)."
            if self.connection_mode == self.ConnectionMode.PANEL:
                errors.update(self._panel_connection_errors())
            if self.mapping:
                from .mapping import validate_mapping

                problems = validate_mapping(self.mapping)
                if problems:
                    errors["mapping"] = problems
        elif self.kind == self.Kind.API:
            if self.connection or self.db_engine or self.db_host:
                errors["connection"] = "Origens API não usam ligação à base de dados."
            if self.mapping:
                errors["mapping"] = "Origens API não usam mapeamento: enviam já no formato canónico."
        if errors:
            raise ValidationError(errors)

    def _panel_connection_errors(self) -> dict:
        errors = {}
        required = "Obrigatório."
        if not self.db_engine:
            errors["db_engine"] = required
        elif self.db_engine == self.Engine.URL:
            if not self.db_url_encrypted:
                errors["db_engine"] = "Indique o URL de ligação."
        elif self.db_engine == self.Engine.SQLITE:
            if not self.db_name:
                errors["db_name"] = "Indique o caminho do ficheiro."
        else:
            if not self.db_host:
                errors["db_host"] = required
            if not self.db_name:
                errors["db_name"] = required
            if not self.db_user and self.db_engine != self.Engine.MSSQL:
                errors["db_user"] = required
        if self.db_options:
            from host_connector.config import HostConfigError, _parse_options

            try:
                _parse_options(self.db_options, "Opções")
            except HostConfigError as exc:
                errors["db_options"] = str(exc)
        return errors

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
