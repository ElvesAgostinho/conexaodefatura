"""Chaves privadas RSA carregadas no painel (PEM ou PFX/P12), segundo a documentação da AGT:
tipo RSA, mínimo 2048 bits.

A chave é convertida para PEM (PKCS#8, sem senha) e guardada CIFRADA na base de dados
(config.crypto). A senha do ficheiro original só é usada para o abrir e não é guardada.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12

MIN_BITS = 2048
MAX_FILE_BYTES = 64 * 1024


class KeyFileError(ValueError):
    pass


@dataclass
class LoadedKey:
    pem: str          # PKCS#8 sem senha (a guardar cifrado)
    bits: int
    fingerprint: str  # SHA-256 da chave pública (para identificar a chave sem a revelar)

    @property
    def info(self) -> str:
        return f"RSA {self.bits} bits · impressão digital {self.fingerprint[:16]}…"


def load_private_key(data: bytes, password: str = "") -> LoadedKey:
    if not data:
        raise KeyFileError("Ficheiro vazio.")
    if len(data) > MAX_FILE_BYTES:
        raise KeyFileError("Ficheiro demasiado grande para uma chave privada.")
    secret = password.encode("utf-8") if password else None
    key = None
    try:
        if b"-----BEGIN" in data:
            key = serialization.load_pem_private_key(data, password=secret)
        else:
            try:
                key, _cert, _extra = pkcs12.load_key_and_certificates(data, secret)
            except ValueError:
                key = serialization.load_der_private_key(data, password=secret)
    except TypeError as exc:  # senha em falta ou a mais
        raise KeyFileError("A chave está protegida por senha: indique a senha do ficheiro.") from exc
    except ValueError as exc:
        raise KeyFileError("Não foi possível abrir a chave (formato inválido ou senha errada).") from exc
    if key is None:
        raise KeyFileError("O ficheiro não contém uma chave privada.")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KeyFileError("A AGT exige uma chave RSA.")
    if key.key_size < MIN_BITS:
        raise KeyFileError(f"A chave tem {key.key_size} bits; a AGT exige no mínimo {MIN_BITS}.")
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode("ascii")
    public_der = key.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    return LoadedKey(pem=pem, bits=key.key_size, fingerprint=hashlib.sha256(public_der).hexdigest())


def private_key_from_pem(pem: str) -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(pem.encode("ascii"), password=None)
