"""Assinatura JWS compacta RS256 (RSA + SHA-256, Base64URL), como pede a AGT para
jwsSoftwareSignature e jwsDocumentSignature. Cabeçalho igual ao dos exemplos oficiais:
{"typ":"JOSE","alg":"RS256"}.
"""

from __future__ import annotations

import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HEADER = {"typ": "JOSE", "alg": "RS256"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign_rs256(payload: dict, key: rsa.RSAPrivateKey) -> str:
    signing_input = f"{b64url(_json(HEADER))}.{b64url(_json(payload))}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode('ascii')}.{b64url(signature)}"


def verify_rs256(token: str, public_key: rsa.RSAPublicKey) -> dict:
    """Verifica a assinatura e devolve o conteúdo. Lança ValueError se não for válida."""
    try:
        header, payload, signature = token.split(".")
    except ValueError as exc:
        raise ValueError("JWS mal formado.") from exc
    if json.loads(b64url_decode(header)).get("alg") != "RS256":
        raise ValueError("Algoritmo não suportado.")
    try:
        public_key.verify(b64url_decode(signature), f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(),
                          hashes.SHA256())
    except InvalidSignature as exc:
        raise ValueError("Assinatura inválida.") from exc
    return json.loads(b64url_decode(payload))
