"""Ligação assistida a um servidor detetado (sempre com autorização explícita do cliente).

1. server_overview()   entra no servidor (autenticação Windows ou credenciais de
                       administrador dadas UMA vez e nunca guardadas) e lista as bases de
                       dados, com uma pontuação de "parece ter faturas" (nomes de tabelas
                       e colunas). Tudo em só leitura.
2. provisioning_plan() prepara o SQL que cria um utilizador SÓ DE LEITURA para o Gateway.
                       O cliente vê exatamente o que vai ser executado antes de aprovar.
3. apply_plan()        executa esse SQL com a ligação de administrador.
4. verify_read_only()  entra com o novo utilizador e confirma que não consegue escrever.

Os nomes (base, utilizador) são validados contra listas reais/padrões fixos e citados
pelo dialeto; a senha é gerada só com letras e números. Não há SQL livre do utilizador.
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text

from .config import HostConfigError, HostConnectionConfig
from .connection import HostDatabase
from .permissions import check_permissions

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,29}$")
DEFAULT_USERNAME = "gateway_leitura"
MAX_DATABASES = 60
SYSTEM_DATABASES = {
    "mssql": {"master", "model", "msdb", "tempdb", "distribution", "ssisdb", "dwconfiguration", "dwdiagnostics",
              "dwqueue", "reportserver", "reportservertempdb"},
    "postgresql": {"postgres"},
    "mysql": {"mysql", "sys", "information_schema", "performance_schema"},
    "mariadb": {"mysql", "sys", "information_schema", "performance_schema"},
}
PROVISIONING_ENGINES = ("mssql", "postgresql", "mysql", "mariadb")

# Palavras que costumam aparecer em sistemas de faturação (PT/EN). Só servem para ordenar
# sugestões: a escolha final é sempre do cliente.
TABLE_WORDS = re.compile(r"(?i)(fatur|factur|invoice|document|doc_|docs|venda|sale|recibo|receipt|billing|nota)")
COLUMN_WORDS = re.compile(r"(?i)(nif|contribuinte|taxid|vat|iva|imposto|tax|total|serie|series|numdoc|docnum|invoice)")


class ProvisioningError(Exception):
    pass


@dataclass
class DatabaseInfo:
    name: str
    score: int = 0
    tables: int = 0
    hints: list[str] = field(default_factory=list)

    @property
    def likely(self) -> bool:
        return self.score >= 3


@dataclass
class ServerOverview:
    version: str = ""
    login: str = ""
    can_create_logins: bool = False
    sql_logins_allowed: bool | None = None  # SQL Server: modo misto ativo?
    databases: list[DatabaseInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        core = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in core) and any(c.isupper() for c in core) and any(c.isdigit() for c in core):
            return f"Gw{core}9x"


def admin_config(engine: str, host: str, *, port=None, database: str = "", user: str = "", password: str = "",
                 trust_server_certificate: bool = True) -> HostConnectionConfig:
    """Ligação de administrador. SQL Server sem utilizador = autenticação Windows."""
    defaults = {"postgresql": "postgres", "mysql": "", "mariadb": "", "mssql": "master"}
    name = database or defaults.get(engine, "")
    if engine in ("mysql", "mariadb") and not name:
        name = "information_schema"
    return HostConnectionConfig.from_values(
        engine=engine, host=host, port=port, name=name, user=user, password=password,
        trust_server_certificate=trust_server_certificate, connect_timeout=8,
    )


# ------------------------------------------------------------ visão geral


def server_overview(config: HostConnectionConfig) -> ServerOverview:
    db = HostDatabase(config)
    overview = ServerOverview()
    try:
        engine = config.engine
        if engine == "mssql":
            row = db.fetch_all(
                "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(40)) AS version, "
                "SUSER_SNAME() AS login, CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int) AS windows_only, "
                "HAS_PERMS_BY_NAME(NULL, NULL, 'ALTER ANY LOGIN') AS can_logins, "
                "IS_SRVROLEMEMBER('sysadmin') AS is_admin")[0]
            overview.version, overview.login = str(row["version"]), str(row["login"])
            overview.sql_logins_allowed = not row["windows_only"]
            overview.can_create_logins = bool(row["can_logins"]) or bool(row["is_admin"])
            names = [r["name"] for r in db.fetch_all(
                "SELECT name FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' "
                "AND HAS_DBACCESS(name) = 1 ORDER BY name", max_rows=500)]
        elif engine == "postgresql":
            row = db.fetch_all("SELECT version() AS version, current_user AS login, "
                               "(SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user) AS can")[0]
            overview.version, overview.login, overview.can_create_logins = row["version"], row["login"], bool(row["can"])
            names = [r["datname"] for r in db.fetch_all(
                "SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY datname", max_rows=500)]
        elif engine in ("mysql", "mariadb"):
            row = db.fetch_all("SELECT VERSION() AS version, CURRENT_USER() AS login")[0]
            overview.version, overview.login = row["version"], row["login"]
            grants = " ".join(str(list(r.values())[0]) for r in db.fetch_all("SHOW GRANTS", max_rows=200))
            overview.can_create_logins = bool(re.search(r"(?i)ALL PRIVILEGES ON \*\.\*|CREATE USER", grants))
            names = [r["name"] for r in db.fetch_all(
                "SELECT schema_name AS name FROM information_schema.schemata ORDER BY schema_name", max_rows=500)]
        else:
            raise ProvisioningError(f"A listagem automática ainda não está disponível para {engine}.")

        system = SYSTEM_DATABASES.get(engine, set())
        names = [n for n in names if n.lower() not in system][:MAX_DATABASES]
        for name in names:
            overview.databases.append(_score_database(db, config, name, overview.warnings))
        overview.databases.sort(key=lambda d: (-d.score, d.name.lower()))
    except ProvisioningError:
        raise
    except Exception as exc:  # noqa: BLE001 - mensagem sem segredos
        raise ProvisioningError(db.safe_error(exc)) from exc
    finally:
        db.dispose()
    return overview


def _score_database(db: HostDatabase, config: HostConnectionConfig, name: str, warnings: list[str]) -> DatabaseInfo:
    info = DatabaseInfo(name)
    try:
        if config.engine == "mssql":
            quoted = db.engine.dialect.identifier_preparer.quote_identifier(name)
            rows = db.fetch_all(f"SELECT TABLE_NAME AS t, COLUMN_NAME AS c FROM {quoted}.INFORMATION_SCHEMA.COLUMNS",
                                max_rows=20000)
        elif config.engine in ("mysql", "mariadb"):
            rows = db.fetch_all("SELECT table_name AS t, column_name AS c FROM information_schema.columns "
                                "WHERE table_schema = :db", {"db": name}, max_rows=20000)
        else:  # postgresql: o catálogo é por base de dados
            per_db = HostDatabase(HostConnectionConfig.from_values(
                engine="postgresql", host=config.host or "", port=config.port, name=name, user=config.user or "",
                password=config.password or "", connect_timeout=8))
            try:
                rows = per_db.fetch_all(
                    "SELECT table_name AS t, column_name AS c FROM information_schema.columns "
                    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')", max_rows=20000)
            finally:
                per_db.dispose()
    except Exception as exc:  # noqa: BLE001 - base sem acesso: continua com as outras
        warnings.append(f"{name}: sem acesso ao catálogo ({type(getattr(exc, 'orig', exc)).__name__}).")
        return info
    tables = {}
    for row in rows:
        tables.setdefault(str(row["t"]), set()).add(str(row["c"]))
    info.tables = len(tables)
    for table, columns in tables.items():
        table_hit = bool(TABLE_WORDS.search(table))
        column_hits = {c for c in columns if COLUMN_WORDS.search(c)}
        points = (2 if table_hit else 0) + min(len(column_hits), 4)
        if points >= 3:
            info.hints.append(table)
        info.score += points
    info.hints = sorted(info.hints)[:6]
    return info


# ----------------------------------------------------------- utilizador


@dataclass
class ProvisioningPlan:
    engine: str
    database: str
    username: str
    password: str
    login_host: str
    statements: list[str]

    def display(self) -> str:
        """SQL a mostrar ao cliente, com a senha escondida."""
        return ";\n".join(s.replace(self.password, "••••••••") for s in self.statements) + ";"


def provisioning_plan(config: HostConnectionConfig, database: str, *, username: str = DEFAULT_USERNAME,
                      password: str | None = None, known_databases=(), login_host: str = "localhost") -> ProvisioningPlan:
    engine = config.engine
    if engine not in PROVISIONING_ENGINES:
        raise ProvisioningError(f"A criação automática do utilizador não está disponível para {engine}. "
                                "Use o guia de ligação para o criar manualmente.")
    if not USERNAME_RE.match(username):
        raise ProvisioningError("Nome de utilizador inválido (letras, números e _, 3 a 30 caracteres).")
    if database not in known_databases:
        raise ProvisioningError("A base de dados escolhida não existe neste servidor.")
    if not re.fullmatch(r"[A-Za-z0-9%._-]{1,60}", login_host):
        raise ProvisioningError("Origem do utilizador inválida.")
    password = password or generate_password()
    if not re.fullmatch(r"[A-Za-z0-9]{12,64}", password):
        raise ProvisioningError("Senha gerada inválida.")

    q = _quoter(engine)
    if engine == "mssql":
        db, user = q(database), q(username)
        statements = [
            f"IF SUSER_ID(N'{username}') IS NULL CREATE LOGIN {user} WITH PASSWORD = N'{password}', "
            f"CHECK_POLICY = ON, DEFAULT_DATABASE = {db}",
            f"IF SUSER_ID(N'{username}') IS NOT NULL ALTER LOGIN {user} WITH PASSWORD = N'{password}'",
            f"USE {db}",
            f"IF USER_ID(N'{username}') IS NULL CREATE USER {user} FOR LOGIN {user}",
            f"ALTER ROLE db_datareader ADD MEMBER {user}",
        ]
    elif engine == "postgresql":
        user = q(username)
        statements = [
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{username}') THEN "
            f"CREATE ROLE {user} LOGIN PASSWORD '{password}'; ELSE ALTER ROLE {user} LOGIN PASSWORD '{password}'; "
            f"END IF; END $$",
            f"GRANT CONNECT ON DATABASE {q(database)} TO {user}",
            f"GRANT pg_read_all_data TO {user}",
        ]
    else:  # mysql / mariadb
        account = f"'{username}'@'{login_host}'"
        statements = [
            f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY '{password}'",
            f"ALTER USER {account} IDENTIFIED BY '{password}'",
            f"GRANT SELECT ON {q(database)}.* TO {account}",
        ]
    return ProvisioningPlan(engine, database, username, password, login_host, statements)


def _quoter(engine: str):
    if engine == "mssql":
        return lambda name: "[" + name.replace("]", "]]") + "]"
    if engine in ("mysql", "mariadb"):
        return lambda name: "`" + name.replace("`", "``") + "`"
    return lambda name: '"' + name.replace('"', '""') + '"'


def apply_plan(config: HostConnectionConfig, plan: ProvisioningPlan) -> None:
    """Executa o plano com a ligação de ADMINISTRADOR (a única escrita feita pelo Gateway)."""
    if config.engine != plan.engine:
        raise ProvisioningError("O plano não corresponde a este servidor.")
    url = config.sqlalchemy_url()
    if config.engine == "mssql":
        url = url.update_query_dict({"ApplicationIntent": "ReadWrite"})
    engine = create_engine(url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            for statement in plan.statements:
                conn.execute(text(statement.replace(":", "\\:")))
    except Exception as exc:  # noqa: BLE001
        message = str(getattr(exc, "orig", exc)).replace(plan.password, "••••••••")
        if config.password:
            message = message.replace(config.password, "••••••••")
        raise ProvisioningError(f"O servidor recusou a criação do utilizador: {message}") from exc
    finally:
        engine.dispose()


@dataclass
class Verification:
    ok: bool
    message: str
    write_permissions: list[str] = field(default_factory=list)


def verify_read_only(config: HostConnectionConfig) -> Verification:
    db = HostDatabase(config)
    try:
        result = db.test_connection()
        if not result.ok:
            return Verification(False, result.message)
        report = check_permissions(db)
        if not report.read_only:
            return Verification(False, "O utilizador tem permissões de escrita.", report.write_permissions)
        return Verification(True, "Ligação OK e só de leitura.")
    except HostConfigError as exc:
        return Verification(False, str(exc))
    finally:
        db.dispose()
