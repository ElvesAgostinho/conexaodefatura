"""Gera o ficheiro .env de uma instalação nova (usado pelo instalador).

Uso:  python gerar_env.py --saida C:\\GatewayFiscal\\.env --dados C:\\GatewayFiscal\\dados --porta 8000

* Nunca substitui um .env existente (uma reinstalação/atualização mantém chaves e dados).
* Gera chaves secretas novas (Django e cifra das senhas guardadas no painel).
* Aceita os nomes deste computador e os seus endereços IP locais.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import socket
import sys
from pathlib import Path


def local_hosts() -> list[str]:
    hosts = ["localhost", "127.0.0.1"]
    name = socket.gethostname()
    for candidate in (name, name.lower(), socket.getfqdn()):
        if candidate and candidate not in hosts:
            hosts.append(candidate)
    try:
        for info in socket.getaddrinfo(name, None, socket.AF_INET):
            if info[4][0] not in hosts:
                hosts.append(info[4][0])
    except OSError:
        pass
    return hosts


def build_env(data_dir: Path, port: int, hosts: list[str], log_file: Path) -> str:
    secret_key = secrets.token_urlsafe(50)
    fernet_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    origins = ",".join(f"http://{h}:{port}" for h in hosts)
    lines = [
        "# Gerado pelo instalador do Gateway Fiscal. Guarde uma cópia de segurança deste ficheiro:",
        "# sem a GATEWAY_ENCRYPTION_KEY não é possível ler as senhas guardadas no painel.",
        "DJANGO_DEBUG=False",
        f"DJANGO_SECRET_KEY={secret_key}",
        f"GATEWAY_ENCRYPTION_KEY={fernet_key}",
        f"DJANGO_ALLOWED_HOSTS={','.join(hosts)}",
        f"DJANGO_CSRF_TRUSTED_ORIGINS={origins}",
        "# Rede interna sem certificado (http). Com HTTPS (proxy/IIS), mude para True.",
        "DJANGO_HTTPS=False",
        "DJANGO_TIME_ZONE=Africa/Luanda",
        "GATEWAY_DB_ENGINE=sqlite",
        f"GATEWAY_DB_NAME={data_dir / 'gateway.sqlite3'}",
        f"GATEWAY_LOG_FILE={log_file}",
        f"HOST_REPORTS_DIR={data_dir / 'relatorios'}",
        "API_RATE_LIMIT=120/min",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saida", required=True, type=Path)
    parser.add_argument("--dados", required=True, type=Path)
    parser.add_argument("--porta", type=int, default=8000)
    parser.add_argument("--registo", type=Path)
    args = parser.parse_args(argv)
    if args.saida.exists():
        print(f".env já existe: mantido ({args.saida})")
        return 0
    if not 1 <= args.porta <= 65535:
        print("Porta inválida.", file=sys.stderr)
        return 2
    args.dados.mkdir(parents=True, exist_ok=True)
    log_file = args.registo or args.saida.parent / "logs" / "gateway.log"
    args.saida.write_text(build_env(args.dados, args.porta, local_hosts(), log_file), encoding="utf-8")
    print(f".env criado: {args.saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
