"""Cifra de segredos guardados na base de dados do Gateway (ex.: senha de uma BD de origem).

Usa Fernet (AES-128-CBC + HMAC-SHA256). A chave vem de GATEWAY_ENCRYPTION_KEY no .env;
sem ela, é derivada da DJANGO_SECRET_KEY. Mudar qualquer uma destas chaves obriga a
voltar a introduzir as senhas guardadas.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from .env import env_str


class DecryptionError(ValueError):
    pass


def _fernet() -> Fernet:
    key = env_str("GATEWAY_ENCRYPTION_KEY")
    if key:
        return Fernet(key.encode())
    digest = hashlib.sha256(("gateway-fiscal:" + settings.SECRET_KEY).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise DecryptionError(
            "Não foi possível decifrar a senha guardada (a chave de cifra mudou?). Introduza-a de novo."
        ) from exc
