"""Mascaramento de segredos em logs e mensagens de erro."""

import logging
import os
import re

MASK = "***"

# Variáveis cujo valor nunca pode aparecer em logs ou no ecrã.
SECRET_ENV_VARS = (
    "DJANGO_SECRET_KEY",
    "GATEWAY_DB_PASSWORD",
    "GATEWAY_ENCRYPTION_KEY",
    "HOST_DB_PASSWORD",
    "HOST_DB_URL",
    "AGT_CLIENT_SECRET",
    "AGT_PRIVATE_KEY",
    "AGT_PRIVATE_KEY_PASSWORD",
)

# Variáveis com nome por empresa/ligação: HOST_<NOME>_DB_PASSWORD, HOST_<NOME>_DB_URL,
# AGT_<PREFIXO>_CLIENT_SECRET, AGT_<PREFIXO>_PRIVATE_KEY, AGT_<PREFIXO>_PRIVATE_KEY_PASSWORD.
_SECRET_ENV_PATTERN = re.compile(
    r"^(HOST_[A-Z0-9_]+_DB_(PASSWORD|URL)|AGT_[A-Z0-9_]+_(CLIENT_SECRET|PRIVATE_KEY|PRIVATE_KEY_PASSWORD))$"
)


def _secret_env_values() -> list[str]:
    names = set(SECRET_ENV_VARS) | {name for name in os.environ if _SECRET_ENV_PATTERN.match(name.upper())}
    return [os.environ.get(name) for name in names]


# password=..., pwd=..., secret=..., token=... em connection strings / querystrings.
_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|client_secret|token|access_token|api[_-]?key)(\s*[=:]\s*)([^;&\s,'\"]+)"
)
# esquema://utilizador:senha@host
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^:/@\s]+:)([^@\s]+)(@)")
# Authorization: Bearer xxx
_BEARER = re.compile(r"(?i)\b(bearer\s+)[a-z0-9\-._~+/]+=*")


def mask_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    if not text:
        return text
    text = str(text)
    # Mais longos primeiro, para um segredo que contém outro ser mascarado inteiro.
    candidates = sorted((v for v in (*_secret_env_values(), *extra_secrets) if v), key=len, reverse=True)
    for secret in candidates:
        if secret and len(secret) >= 3:
            text = text.replace(secret, MASK)
    text = _URL_CREDENTIALS.sub(rf"\1{MASK}\3", text)
    text = _KEY_VALUE.sub(rf"\1\2{MASK}", text)
    text = _BEARER.sub(rf"\1{MASK}", text)
    return text


class SecretMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = None
        return True
