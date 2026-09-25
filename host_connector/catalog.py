"""Catálogo leve de uma BD de origem para o painel: tabelas, colunas, amostra de linhas e
geração segura de consultas para o assistente de mapeamento.

Segurança: os nomes de tabelas e colunas escolhidos no painel são SEMPRE validados contra
a estrutura real devolvida pelo SGBD e citados pelo próprio dialeto; nunca há SQL livre
vindo do utilizador. As consultas geradas passam ainda pela guarda de só-leitura.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import re

from sqlalchemy import MetaData, Table, func, inspect, select

from .connection import HostDatabase
from .diagnostic_queries import DEFAULT_SCHEMA_ONLY, SYSTEM_SCHEMAS
from .sql_guard import assert_read_only

MAX_TABLES = 5000
SAMPLE_LIMIT = 5
MAX_VALUE_LENGTH = 120


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class TableRef:
    schema: str | None
    name: str
    kind: str = "tabela"

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


def list_tables(db: HostDatabase) -> list[TableRef]:
    """Tabelas e vistas visíveis (sem schemas de sistema), ordenadas por nome."""
    tables: list[TableRef] = []
    with db.connect() as conn:
        inspector = inspect(conn)
        default = inspector.default_schema_name
        single = db.config.engine in DEFAULT_SCHEMA_ONLY
        if single:
            schemas = [default]
        else:
            schemas = [s for s in inspector.get_schema_names()
                       if s not in SYSTEM_SCHEMAS.get(db.config.engine, set()) and not s.startswith("pg_temp")]
        for schema in schemas or [default]:
            arg = None if schema == default else schema
            label = None if single else schema
            for name in inspector.get_table_names(schema=arg):
                tables.append(TableRef(label, name, "tabela"))
            for name in inspector.get_view_names(schema=arg):
                tables.append(TableRef(label, name, "vista"))
            if len(tables) > MAX_TABLES:
                break
    return sorted(tables, key=lambda t: (t.qualified.lower()))[:MAX_TABLES]


def resolve_table(db: HostDatabase, qualified: str, tables: list[TableRef] | None = None) -> TableRef:
    tables = tables if tables is not None else list_tables(db)
    for table in tables:
        if table.qualified == qualified:
            return table
    raise CatalogError(f"A tabela {qualified!r} não existe nesta base de dados.")


def _reflect(conn, ref: TableRef) -> Table:
    inspector = inspect(conn)
    default = inspector.default_schema_name
    schema = None if ref.schema in (None, default) else ref.schema
    return Table(ref.name, MetaData(), schema=schema, autoload_with=conn)


def table_columns(db: HostDatabase, ref: TableRef) -> list[dict[str, Any]]:
    with db.connect() as conn:
        table = _reflect(conn, ref)
        pk = {c.name for c in table.primary_key.columns}
        return [{"name": c.name, "type": _type_name(c.type), "nullable": c.nullable, "pk": c.name in pk}
                for c in table.columns]


def sample_rows(db: HostDatabase, ref: TableRef, limit: int = SAMPLE_LIMIT) -> tuple[list[str], list[list[str]]]:
    with db.connect() as conn:
        table = _reflect(conn, ref)
        rows = conn.execute(select(table).limit(limit)).fetchall()
        names = [c.name for c in table.columns]
    return names, [[_format(value) for value in row] for row in rows]


def _type_name(sa_type) -> str:
    try:
        return str(sa_type)
    except Exception:  # noqa: BLE001 - tipos específicos do dialeto
        return type(sa_type).__name__


def _format(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binário {len(bytes(value))} bytes>"
    text = str(value)
    return text if len(text) <= MAX_VALUE_LENGTH else text[: MAX_VALUE_LENGTH - 1] + "…"


# ------------------------------------------------------------ geração de SQL


def quote_table(db: HostDatabase, ref: TableRef) -> str:
    preparer = db.engine.dialect.identifier_preparer
    name = preparer.quote(ref.name)
    return f"{preparer.quote_schema(ref.schema)}.{name}" if ref.schema else name


def quote_column(db: HostDatabase, column: str) -> str:
    return db.engine.dialect.identifier_preparer.quote(column)


_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def build_select(db: HostDatabase, ref: TableRef, columns: list[str], where_column: str, operator: str,
                 param: str, order_by: str | None, available: set[str],
                 conditions: list[tuple[str, str, list[str]]] | None = None) -> str:
    """SELECT <colunas> FROM <tabela> WHERE <coluna> <op> :param [AND ...] ORDER BY <coluna>.

    `conditions`: filtros extra (coluna, ">=" ou "IN", nomes dos parâmetros). Todas as colunas
    têm de existir em `available` (colunas reais da tabela); os valores vão sempre em parâmetros.
    """
    if operator not in (">", "="):
        raise CatalogError("Operador inválido.")
    conditions = conditions or []
    wanted = list(dict.fromkeys([*columns, where_column, *([order_by] if order_by else [])]))
    unknown = [c for c in [*wanted, *(c[0] for c in conditions)] if c not in available]
    if unknown:
        raise CatalogError(f"Colunas inexistentes em {ref.qualified}: {', '.join(dict.fromkeys(unknown))}.")
    where = [f"{quote_column(db, where_column)} {operator} :{param}"]
    for column, op, names in conditions:
        if op not in (">=", "IN") or not names or not all(_PARAM_RE.match(n) for n in [*names, param]):
            raise CatalogError("Filtro inválido.")
        if op == ">=":
            where.append(f"{quote_column(db, column)} >= :{names[0]}")
        else:
            where.append(f"{quote_column(db, column)} IN ({', '.join(':' + n for n in names)})")
    sql = (
        f"SELECT {', '.join(quote_column(db, c) for c in wanted)} FROM {quote_table(db, ref)} "
        f"WHERE {' AND '.join(where)}"
        + (f" ORDER BY {quote_column(db, order_by)}" if order_by else "")
    )
    assert_read_only(sql)
    return sql


def distinct_values(db: HostDatabase, ref: TableRef, column: str, limit: int = 50) -> list[tuple[str, int]]:
    """Valores diferentes de uma coluna e quantas vezes aparecem (ex.: tipos de documento)."""
    with db.connect() as conn:
        table = _reflect(conn, ref)
        if column not in table.c:
            raise CatalogError(f"A coluna {column!r} não existe em {ref.qualified}.")
        col = table.c[column]
        count = func.count().label("n")
        rows = conn.execute(select(col, count).group_by(col).order_by(count.desc()).limit(limit)).fetchall()
    return [(str(value).strip(), int(n)) for value, n in rows if value is not None and str(value).strip()]
