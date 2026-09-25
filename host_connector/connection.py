"""Ligação SOMENTE LEITURA à base de dados do HOST.

Proteções aplicadas (da mais forte para a mais fraca):
  1. Utilizador da BD só com permissão SELECT (configuração do DBA; verificada
     por host_connector.permissions).
  2. Sessão em modo leitura no próprio SGBD, quando o motor o permite.
  3. Guarda de SQL (sql_guard) em todas as consultas feitas pela aplicação.
  4. Nenhuma transação é confirmada: a ligação faz sempre rollback no fim.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, literal, select, text
from sqlalchemy.engine import Connection, Engine

from config.logging_utils import mask_secrets

from .config import HostConnectionConfig
from .sql_guard import assert_read_only

DEFAULT_MAX_ROWS = 1000

try:  # O pooling do gestor ODBC mantém sessões abertas no servidor do cliente depois de
    # fechadas pelo Gateway. Desligado: cada ligação fecha de facto quando termina.
    import pyodbc

    pyodbc.pooling = False
except ImportError:  # pragma: no cover - SQL Server não usado neste ambiente
    pass


@dataclass
class ConnectionResult:
    ok: bool
    message: str
    server_version: str | None = None
    elapsed_ms: int | None = None
    read_only_measures: list[str] = field(default_factory=list)


class HostDatabase:
    def __init__(self, config: HostConnectionConfig):
        self.config = config
        self._engine: Engine | None = None
        self.read_only_measures: list[str] = []

    # ------------------------------------------------------------------ engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = self._create_engine()
        return self._engine

    def _create_engine(self) -> Engine:
        cfg = self.config
        measures = ["Guarda de SQL: só SELECT/WITH", "Sem commit: rollback no fim de cada ligação"]

        if cfg.engine == "sqlite" and not cfg.url_override:
            uri = Path(cfg.name).resolve().as_uri() + "?mode=ro"
            measures.append("SQLite aberto com mode=ro")
            engine = create_engine(
                "sqlite://",
                creator=lambda: sqlite3.connect(uri, uri=True, timeout=cfg.connect_timeout),
            )
            self.read_only_measures = measures
            return engine

        connect_args: dict[str, Any] = {}
        if cfg.engine == "mssql":
            connect_args["timeout"] = cfg.connect_timeout
            measures.append("SQL Server: ApplicationIntent=ReadOnly")
        elif cfg.engine == "postgresql":
            connect_args["connect_timeout"] = cfg.connect_timeout
            connect_args["options"] = "-c default_transaction_read_only=on"
            measures.append("PostgreSQL: default_transaction_read_only=on")
        elif cfg.engine in ("mysql", "mariadb"):
            connect_args["connect_timeout"] = cfg.connect_timeout
            connect_args["init_command"] = "SET SESSION TRANSACTION READ ONLY"
            measures.append("MySQL/MariaDB: SET SESSION TRANSACTION READ ONLY")
        elif cfg.engine == "oracle":
            connect_args["tcp_connect_timeout"] = cfg.connect_timeout

        self.read_only_measures = measures
        return create_engine(cfg.sqlalchemy_url(), connect_args=connect_args, pool_pre_ping=True)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # ------------------------------------------------------------- utilização

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """Ligação para leitura. Nunca é feito commit."""
        conn = self.engine.connect()
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def fetch_all(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> list[dict[str, Any]]:
        """Executa uma consulta parametrizada de leitura e devolve linhas como dicts.

        Os valores vindos do utilizador têm de ir em `params` (nunca concatenados
        no SQL) para evitar SQL injection.
        """
        assert_read_only(sql)
        with self.connect() as conn:
            result = conn.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings().fetchmany(max_rows)]

    def test_connection(self) -> ConnectionResult:
        started = time.perf_counter()
        try:
            with self.connect() as conn:
                conn.execute(select(literal(1))).scalar_one()
                version = conn.dialect.server_version_info
        except Exception as exc:  # noqa: BLE001 - qualquer falha é reportada ao utilizador
            return ConnectionResult(
                ok=False,
                message=self.safe_error(exc),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                read_only_measures=self.read_only_measures,
            )
        return ConnectionResult(
            ok=True,
            message="CONEXÃO OK",
            server_version=".".join(str(part) for part in version) if version else None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            read_only_measures=self.read_only_measures,
        )

    def safe_error(self, exc: BaseException) -> str:
        """Mensagem de erro sem senhas nem connection strings."""
        original = getattr(exc, "orig", None) or exc
        message = f"{type(original).__name__}: {original}"
        secrets = (self.config.password,) if self.config.password else ()
        return mask_secrets(message, extra_secrets=secrets)
