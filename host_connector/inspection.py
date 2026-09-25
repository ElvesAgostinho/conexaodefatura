"""Descoberta da estrutura REAL da base de dados do HOST (Fase 4).

Lê apenas metadados: bases de dados, schemas, tabelas, vistas, colunas, tipos,
chaves primárias, chaves estrangeiras, índices e estimativa de linhas.
Não lê dados de negócio.

A secção "candidates" é só uma SUGESTÃO por palavras-chave para acelerar a
análise manual. Não substitui a confirmação do mapeamento (Fase 6).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine.reflection import Inspector, ObjectKind

from .connection import HostDatabase
from .diagnostic_queries import DEFAULT_SCHEMA_ONLY, LIST_DATABASES, ROW_ESTIMATES, SYSTEM_SCHEMAS

# Papéis que o mapeamento terá de identificar, com palavras-chave (PT/EN/ES)
# procuradas nos nomes de tabelas e colunas.
ROLE_KEYWORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "cabecalho_fatura": {
        "table": ("fact", "factur", "fatur", "invoice", "docum", "venda", "sale", "bill", "folio", "cabec", "header"),
        "column": ("hash", "atcud", "doctype", "tipodoc", "numdoc", "docnum", "invoiceno", "grosstotal", "nettotal"),
    },
    "linhas_fatura": {
        "table": ("linha", "line", "detal", "detail", "item", "mov"),
        "column": ("linha", "lineno", "quantidade", "quantity", "qtd", "unitprice", "precounit"),
    },
    "cliente": {
        "table": ("client", "customer", "cust", "guest", "hosped", "entidad", "entity", "terceir"),
        "column": ("nif", "contribuinte", "taxid", "customerid", "morada", "address"),
    },
    "artigos": {
        "table": ("artig", "product", "produt", "article", "servic", "catalog"),
        "column": ("productcode", "codartigo", "artigo", "unidade", "unit"),
    },
    "impostos": {
        "table": ("iva", "tax", "impost", "vat", "taxa"),
        "column": ("taxcode", "taxpercentage", "taxa", "isencao", "exemption"),
    },
    "pagamentos": {
        "table": ("pagam", "payment", "pay", "receb", "recib", "tender", "caixa"),
        "column": ("paymentmechanism", "meiopag", "valorpago", "amountpaid"),
    },
    "series": {
        "table": ("serie", "series", "numerad", "sequen"),
        "column": ("serie", "series", "prefix", "prefixo"),
    },
    "anulacoes_notas_credito": {
        "table": ("anul", "cancel", "void", "credit", "credito", "estorn", "devol"),
        "column": ("anulad", "cancel", "void", "motivo", "reason", "reference", "referencia"),
    },
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Palavras-chave curtas ("fact", "sale", "iva", "nif"...) só contam no início de
# uma palavra do nome, para "sysalerts" não parecer "sale" nem "Ativa" parecer "iva".
_SHORT_KEYWORD = 4
_NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _keyword_hits(name: str, keywords: tuple[str, ...]) -> list[str]:
    normalized = _normalize(name)
    words = [w.lower() for w in _NAME_WORDS.findall(name)]
    return [
        kw for kw in keywords
        if (kw in normalized if len(kw) > _SHORT_KEYWORD else any(w.startswith(kw) for w in words))
    ]


def _type_to_str(sa_type: Any) -> str:
    try:
        return str(sa_type)
    except Exception:  # noqa: BLE001 - alguns tipos não compilam fora do dialeto
        return type(sa_type).__name__


class SchemaInspector:
    def __init__(self, db: HostDatabase):
        self.db = db
        self.warnings: list[str] = []

    # --------------------------------------------------------------- público

    def run(self, schemas: list[str] | None = None, *, row_counts: bool = True) -> dict[str, Any]:
        engine_name = self.db.config.engine
        with self.db.connect() as conn:
            inspector: Inspector = inspect(conn)
            default_schema = inspector.default_schema_name
            target_schemas = schemas or self._default_schemas(inspector, engine_name, default_schema)

            estimates = self._row_estimates() if row_counts else {}
            schema_reports = [
                self._inspect_schema(inspector, schema, default_schema, estimates) for schema in target_schemas
            ]
            version = conn.dialect.server_version_info

        schema_reports = [s for s in schema_reports if s["tables"] or s["views"]]
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "connection": self.db.config.describe(),
            "dialect": self.db.engine.dialect.name,
            "server_version": ".".join(map(str, version)) if version else None,
            "default_schema": default_schema,
            "databases": self._list_databases(),
            "schemas": schema_reports,
            "summary": {
                "schemas": len(schema_reports),
                "tables": sum(len(s["tables"]) for s in schema_reports),
                "views": sum(len(s["views"]) for s in schema_reports),
            },
            "candidates": suggest_candidates(schema_reports),
            "warnings": self.warnings,
        }
        return report

    # --------------------------------------------------------------- privado

    def _default_schemas(self, inspector: Inspector, engine_name: str, default_schema: str | None) -> list[str]:
        if engine_name in DEFAULT_SCHEMA_ONLY:
            return [default_schema]
        system = SYSTEM_SCHEMAS.get(engine_name, set())
        try:
            names = inspector.get_schema_names()
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"Não foi possível listar schemas: {self.db.safe_error(exc)}")
            return [default_schema]
        return [name for name in names if name not in system and not name.startswith("pg_temp")]

    def _list_databases(self) -> list[str]:
        sql = LIST_DATABASES.get(self.db.config.engine)
        if not sql:
            return []
        try:
            return [row["name"] for row in self.db.fetch_all(sql, max_rows=10_000)]
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"Não foi possível listar bases de dados: {self.db.safe_error(exc)}")
            return []

    def _row_estimates(self) -> dict[tuple[str, str], int | None]:
        sql = ROW_ESTIMATES.get(self.db.config.engine)
        if not sql:
            return {}
        try:
            rows = self.db.fetch_all(sql, max_rows=1_000_000)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"Estimativa de linhas indisponível: {self.db.safe_error(exc)}")
            return {}
        estimates = {}
        for row in rows:
            count = row["row_count"]
            estimates[(str(row["schema_name"]).lower(), str(row["table_name"]).lower())] = (
                int(count) if count is not None and int(count) >= 0 else None
            )
        return estimates

    def _safe(self, label: str, func, default):
        try:
            return func()
        except NotImplementedError:
            return default
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"{label}: {self.db.safe_error(exc)}")
            return default

    def _inspect_schema(self, inspector: Inspector, schema: str | None, default_schema: str | None, estimates):
        # schema=None significa o schema por omissão da ligação.
        arg = None if schema == default_schema else schema
        label = schema or default_schema or "(default)"

        table_names = self._safe(f"Tabelas de {label}", lambda: inspector.get_table_names(schema=arg), [])
        view_names = self._safe(f"Vistas de {label}", lambda: inspector.get_view_names(schema=arg), [])
        if not table_names and not view_names:
            return {"name": label, "tables": [], "views": []}

        kinds = ObjectKind.TABLE | ObjectKind.VIEW
        columns = self._safe(f"Colunas de {label}", lambda: inspector.get_multi_columns(schema=arg, kind=kinds), {})
        pks = self._safe(f"Chaves primárias de {label}", lambda: inspector.get_multi_pk_constraint(schema=arg), {})
        fks = self._safe(f"Chaves estrangeiras de {label}", lambda: inspector.get_multi_foreign_keys(schema=arg), {})
        indexes = self._safe(f"Índices de {label}", lambda: inspector.get_multi_indexes(schema=arg), {})
        comments = self._safe(
            f"Comentários de {label}", lambda: inspector.get_multi_table_comment(schema=arg, kind=kinds), {}
        )

        def build(name: str, kind: str) -> dict[str, Any]:
            key = (arg, name)
            pk = pks.get(key) or {}
            return {
                "schema": label,
                "name": name,
                "kind": kind,
                "row_estimate": estimates.get((str(label).lower(), name.lower())),
                "comment": (comments.get(key) or {}).get("text"),
                "columns": [
                    {
                        "name": col["name"],
                        "type": _type_to_str(col["type"]),
                        "nullable": col.get("nullable"),
                        "default": None if col.get("default") is None else str(col.get("default")),
                        "autoincrement": col.get("autoincrement") is True,
                        "comment": col.get("comment"),
                    }
                    for col in columns.get(key, [])
                ],
                "primary_key": pk.get("constrained_columns") or [],
                "foreign_keys": [
                    {
                        "name": fk.get("name"),
                        "columns": fk.get("constrained_columns") or [],
                        "referred_schema": fk.get("referred_schema") or label,
                        "referred_table": fk.get("referred_table"),
                        "referred_columns": fk.get("referred_columns") or [],
                    }
                    for fk in fks.get(key, [])
                ],
                "indexes": [
                    {"name": ix.get("name"), "columns": ix.get("column_names") or [], "unique": bool(ix.get("unique"))}
                    for ix in indexes.get(key, [])
                ],
            }

        return {
            "name": label,
            "tables": [build(name, "table") for name in sorted(table_names)],
            "views": [build(name, "view") for name in sorted(view_names)],
        }


def suggest_candidates(schema_reports: list[dict[str, Any]], top: int = 5) -> dict[str, list[dict[str, Any]]]:
    """Ordena tabelas/vistas por semelhança de nomes com cada papel. Apenas sugestão."""
    objects = [obj for schema in schema_reports for obj in (*schema["tables"], *schema["views"])]
    candidates: dict[str, list[dict[str, Any]]] = {}

    for role, keywords in ROLE_KEYWORDS.items():
        scored = []
        for obj in objects:
            table_hits = _keyword_hits(obj["name"], keywords["table"])
            column_hits = sorted({kw for col in obj["columns"] for kw in _keyword_hits(col["name"], keywords["column"])})
            score = 3 * len(table_hits) + len(column_hits)
            if table_hits and obj.get("row_estimate"):
                score += 1  # tabelas com dados pesam mais do que tabelas vazias
            if score > 0 and (table_hits or len(column_hits) >= 2):
                scored.append({
                    "object": f"{obj['schema']}.{obj['name']}",
                    "kind": obj["kind"],
                    "score": score,
                    "row_estimate": obj.get("row_estimate"),
                    "matched_table_keywords": table_hits,
                    "matched_column_keywords": column_hits,
                })
        scored.sort(key=lambda item: (-item["score"], item["object"]))
        candidates[role] = scored[:top]
    return candidates
