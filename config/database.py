"""Configuração da base de dados PRÓPRIA do Gateway (não confundir com a BD do HOST).

O motor é escolhido por GATEWAY_DB_ENGINE, para o Gateway poder ser instalado
na infraestrutura que cada cliente já tem:

    sqlite      -> apenas desenvolvimento/testes locais
    postgresql  -> recomendado (psycopg)
    mssql       -> SQL Server (mssql-django + pyodbc)
    mysql       -> MySQL / MariaDB (mysqlclient)
    oracle      -> Oracle (oracledb)
"""

from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .env import env_int, env_str

_BACKENDS = {
    "sqlite": "django.db.backends.sqlite3",
    "postgresql": "django.db.backends.postgresql",
    "mssql": "mssql",
    "mysql": "django.db.backends.mysql",
    "mariadb": "django.db.backends.mysql",
    "oracle": "django.db.backends.oracle",
}

_ALIASES = {
    "sqlite3": "sqlite",
    "postgres": "postgresql",
    "pgsql": "postgresql",
    "sqlserver": "mssql",
    "sql_server": "mssql",
}


def gateway_database(base_dir: Path) -> dict:
    engine = env_str("GATEWAY_DB_ENGINE", "sqlite").strip().lower()
    engine = _ALIASES.get(engine, engine)
    if engine not in _BACKENDS:
        raise ImproperlyConfigured(
            f"GATEWAY_DB_ENGINE={engine!r} não suportado. Opções: {', '.join(sorted(_BACKENDS))}."
        )

    if engine == "sqlite":
        return {
            "ENGINE": _BACKENDS[engine],
            "NAME": env_str("GATEWAY_DB_NAME", str(base_dir / "gateway.sqlite3")),
            # O servidor web e as tarefas automáticas escrevem ao mesmo tempo: WAL e espera.
            "OPTIONS": {"timeout": 30, "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"},
        }

    config = {
        "ENGINE": _BACKENDS[engine],
        "NAME": env_str("GATEWAY_DB_NAME", required=True),
        "USER": env_str("GATEWAY_DB_USER", ""),
        "PASSWORD": env_str("GATEWAY_DB_PASSWORD", ""),
        "HOST": env_str("GATEWAY_DB_HOST", "localhost"),
        "PORT": str(env_int("GATEWAY_DB_PORT", "") or ""),
        "CONN_MAX_AGE": env_int("GATEWAY_DB_CONN_MAX_AGE", 60),
        "OPTIONS": {},
    }

    if engine == "mssql":
        config["OPTIONS"] = {
            "driver": env_str("GATEWAY_DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
            "extra_params": env_str("GATEWAY_DB_EXTRA_PARAMS", ""),
        }
    elif engine == "postgresql":
        sslmode = env_str("GATEWAY_DB_SSLMODE")
        if sslmode:
            config["OPTIONS"]["sslmode"] = sslmode
    elif engine in ("mysql", "mariadb"):
        config["OPTIONS"] = {"charset": "utf8mb4"}

    return config
