"""Guarda de SQL: só deixa passar consultas de leitura para a BD do HOST.

É uma camada de defesa na aplicação. A garantia definitiva tem de vir da própria
BD: o utilizador configurado em HOST_DB_USER deve ter APENAS permissão SELECT.
"""

import re

ALLOWED_FIRST_KEYWORDS = frozenset({"SELECT", "WITH"})

FORBIDDEN_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "TRUNCATE",
    "ALTER", "DROP", "CREATE", "RENAME",
    "GRANT", "REVOKE", "DENY",
    "EXEC", "EXECUTE", "CALL", "DECLARE", "SET", "USE", "WAITFOR",
    "INTO",  # SELECT ... INTO cria tabelas em SQL Server
    "BACKUP", "RESTORE", "DBCC", "SHUTDOWN", "KILL",
    "BULK", "LOAD", "OPENROWSET", "OPENQUERY", "OPENDATASOURCE",
    "ATTACH", "DETACH", "VACUUM", "REINDEX", "PRAGMA",
    "LOCK", "HANDLER", "COPY",
})

# Comentários, literais e identificadores entre aspas numa só passagem (o que
# começar primeiro ganha), para que "--" dentro de uma string não seja comentário.
# Nada disto conta como palavra-chave.
_NON_CODE = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\[[^\]]*\]|`[^`]*`",
    re.DOTALL,
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")


class ReadOnlyViolation(PermissionError):
    """Tentativa de executar SQL que não é de leitura na BD do HOST."""


def assert_read_only(sql: str) -> None:
    if not sql or not sql.strip():
        raise ReadOnlyViolation("Consulta vazia.")

    cleaned = _NON_CODE.sub(" ", sql).strip()
    if "'" in cleaned or "/*" in cleaned:
        raise ReadOnlyViolation("Consulta com literal ou comentário não terminado.")

    body = cleaned.rstrip(";").strip()
    if ";" in body:
        raise ReadOnlyViolation("Só é permitida uma instrução por consulta.")

    words = [w.upper() for w in _WORD.findall(body)]
    if not words or words[0] not in ALLOWED_FIRST_KEYWORDS:
        raise ReadOnlyViolation("Só são permitidas consultas SELECT/WITH na BD do HOST.")

    forbidden = sorted(set(words) & FORBIDDEN_KEYWORDS)
    if forbidden:
        raise ReadOnlyViolation(
            f"Consulta bloqueada: contém instruções não permitidas ({', '.join(forbidden)})."
        )
