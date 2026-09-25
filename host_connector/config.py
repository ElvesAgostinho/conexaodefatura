"""Configuração da ligação à base de dados do HOST (acesso SOMENTE LEITURA).

A configuração vem do .env. Está isolada nesta dataclass para que, na fase
multiempresa, possa ser construída a partir do registo de cada Company sem
alterar o resto do conector.

Motores suportados diretamente por HOST_DB_ENGINE:
    mssql       SQL Server (pyodbc + ODBC Driver 17/18)
    postgresql  PostgreSQL (psycopg)
    mysql       MySQL (PyMySQL)
    mariadb     MariaDB (PyMySQL)
    oracle      Oracle (oracledb)
    sqlite      ficheiro SQLite (aberto com mode=ro)

Qualquer outro SGBD com dialeto SQLAlchemy (Firebird, DB2, Informix, Sybase,
Access, ...) pode ser usado com HOST_DB_URL, depois de instalado o respetivo driver.

Ligações com nome (multiempresa): a ligação "default" lê HOST_DB_*; uma ligação
com nome X lê as mesmas variáveis com o prefixo HOST_X_DB_* (ex.: HOST_HOTEL2_DB_HOST).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import URL, make_url

from config.logging_utils import MASK, mask_secrets

SUPPORTED_ENGINES = ("mssql", "postgresql", "mysql", "mariadb", "oracle", "sqlite")

_ALIASES = {
    "sqlserver": "mssql",
    "sql_server": "mssql",
    "postgres": "postgresql",
    "pgsql": "postgresql",
    "sqlite3": "sqlite",
}

_DRIVERNAMES = {
    "mssql": "mssql+pyodbc",
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "oracle": "oracle+oracledb",
}

# SQL Server não tem porta por omissão: instâncias com nome (SERVIDOR\INSTANCIA)
# usam porta dinâmica resolvida pelo SQL Browser, e fixar 1433 impediria a ligação.
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "oracle": 1521}


DEFAULT_CONNECTION = "default"
CONNECTION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}$")


class HostConfigError(ValueError):
    """Configuração do HOST em falta ou inválida."""


def env_prefix(connection: str | None = DEFAULT_CONNECTION) -> str:
    """Prefixo das variáveis de uma ligação: "HOST_DB_" ou "HOST_<NOME>_DB_"."""
    connection = (connection or DEFAULT_CONNECTION).strip()
    if connection.lower() == DEFAULT_CONNECTION:
        return "HOST_DB_"
    if not CONNECTION_NAME_RE.match(connection):
        raise HostConfigError(
            f"Nome de ligação HOST inválido: {connection!r} (letras, números e _, a começar por letra)."
        )
    return f"HOST_{connection.upper()}_DB_"


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _get_bool(name: str, default: bool) -> bool:
    value = _get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "sim"}


def _parse_options(raw: str | None, var: str = "HOST_DB_OPTIONS") -> dict[str, str]:
    """HOST_DB_OPTIONS no formato "chave=valor;chave=valor"."""
    options: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if not part.strip():
            continue
        if "=" not in part:
            raise HostConfigError(f"{var} inválido (esperado chave=valor): {part!r}")
        key, value = part.split("=", 1)
        options[key.strip()] = value.strip()
    return options


@dataclass(frozen=True)
class HostConnectionConfig:
    engine: str
    host: str | None = None
    port: int | None = None
    name: str | None = None
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    odbc_driver: str = "ODBC Driver 18 for SQL Server"
    trust_server_certificate: bool = False
    connect_timeout: int = 10
    options: dict[str, str] = field(default_factory=dict)
    url_override: str | None = field(default=None, repr=False)
    env_prefix: str = "HOST_DB_"

    @classmethod
    def from_env(cls, connection: str | None = DEFAULT_CONNECTION) -> HostConnectionConfig:
        p = env_prefix(connection)
        url_override = _get(f"{p}URL")
        engine = (_get(f"{p}ENGINE") or "").lower()
        engine = _ALIASES.get(engine, engine)

        if url_override:
            engine = engine or make_url(url_override).get_backend_name()
        elif not engine:
            raise HostConfigError(f"{p}ENGINE não está definido no .env.")
        elif engine not in SUPPORTED_ENGINES:
            raise HostConfigError(
                f"{p}ENGINE={engine!r} não é suportado diretamente "
                f"({', '.join(SUPPORTED_ENGINES)}). Para outro SGBD use {p}URL."
            )

        port = _get(f"{p}PORT")
        try:
            port_number = int(port) if port else _DEFAULT_PORTS.get(engine)
            timeout = int(_get(f"{p}CONNECT_TIMEOUT", "10"))
        except ValueError as exc:
            raise HostConfigError(f"{p}PORT e {p}CONNECT_TIMEOUT devem ser números inteiros.") from exc

        config = cls(
            engine=engine,
            host=_get(f"{p}HOST"),
            port=port_number,
            name=_get(f"{p}NAME"),
            user=_get(f"{p}USER"),
            password=os.environ.get(f"{p}PASSWORD") or None,
            odbc_driver=_get(f"{p}ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
            trust_server_certificate=_get_bool(f"{p}TRUST_SERVER_CERTIFICATE", False),
            connect_timeout=timeout,
            options=_parse_options(_get(f"{p}OPTIONS"), f"{p}OPTIONS"),
            url_override=url_override,
            env_prefix=p,
        )
        config.validate()
        return config

    def validate(self) -> None:
        p = self.env_prefix
        if self.url_override:
            return
        if not self.name:
            raise HostConfigError(f"{p}NAME não está definido (nome da BD, serviço Oracle ou caminho SQLite).")
        if self.engine == "sqlite":
            if not Path(self.name).is_file():
                raise HostConfigError(f"Ficheiro SQLite do HOST não encontrado: {self.name}")
            return
        if not self.host:
            raise HostConfigError(f"{p}HOST não está definido.")
        # SQL Server sem utilizador usa autenticação integrada do Windows.
        if not self.user and self.engine != "mssql":
            raise HostConfigError(f"{p}USER não está definido.")

    @property
    def uses_windows_auth(self) -> bool:
        return self.engine == "mssql" and not self.user and not self.url_override

    def sqlalchemy_url(self) -> URL:
        """URL SQLAlchemy (contém a senha: nunca imprimir; usar describe())."""
        if self.url_override:
            return make_url(self.url_override)
        if self.engine == "sqlite":
            raise HostConfigError("SQLite é aberto por ficheiro; não usa URL.")

        query = dict(self.options)
        database = self.name
        if self.engine == "mssql":
            query.setdefault("driver", self.odbc_driver)
            # Em clusters Always On encaminha para uma réplica de leitura.
            query.setdefault("ApplicationIntent", "ReadOnly")
            if self.trust_server_certificate:
                query.setdefault("TrustServerCertificate", "yes")
            if self.uses_windows_auth:
                query.setdefault("Trusted_Connection", "yes")
        elif self.engine == "oracle":
            query.setdefault("service_name", self.name)
            database = None

        return URL.create(
            drivername=_DRIVERNAMES[self.engine],
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database,
            query=query,
        )

    def describe(self) -> dict[str, object]:
        """Resumo seguro para mostrar ao utilizador (sem a senha)."""
        if self.engine == "sqlite" and not self.url_override:
            location = self.name
        else:
            # mask_secrets cobre senhas escondidas na querystring (ex.: odbc_connect=...PWD=...).
            location = mask_secrets(
                self.sqlalchemy_url().render_as_string(hide_password=True),
                extra_secrets=(self.password,) if self.password else (),
            )
        return {
            "engine": self.engine,
            "host": self.host,
            "port": self.port,
            "database": self.name,
            "user": self.user or ("(autenticação Windows)" if self.uses_windows_auth else None),
            "password": MASK if self.password else "(vazia)",
            "location": location,
        }
