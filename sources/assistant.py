"""Assistente de mapeamento: escolhas feitas no painel (tabelas e colunas reais) -> mapeamento.

As consultas são geradas a partir de nomes validados contra a estrutura real e citados
pelo dialeto (host_connector.catalog.build_select). O resultado é um mapeamento normal,
que pode depois ser afinado à mão em JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from host_connector.catalog import TableRef, build_select
from host_connector.connection import HostDatabase
from invoices.canonical import CanonicalError, parse_document, validate_document

from .mapping import cursor_value, row_value, rows_to_payload, validate_mapping

HEADER_LABELS = {
    "source_document_id": "ID do documento na origem",
    "document_type": "Tipo de documento (FT, FR, NC, ...)",
    "series": "Série",
    "document_number": "Número do documento",
    "document_date": "Data do documento",
    "customer_name": "Nome do cliente",
    "customer_nif": "NIF do cliente",
    "currency": "Moeda",
    "subtotal": "Subtotal (sem imposto)",
    "tax_amount": "Total do imposto",
    "total": "Total do documento",
    "document_hash": "Hash / assinatura da fatura",
    "hash_control": "Controlo do hash (versão da chave)",
}
LINE_LABELS = {
    "line_number": "Número da linha",
    "product_code": "Código do artigo",
    "description": "Descrição",
    "quantity": "Quantidade",
    "unit_price": "Preço unitário",
    "discount": "Desconto",
    "tax_rate": "Taxa de imposto (%)",
    "tax_amount": "Valor do imposto",
    "tax_exemption_code": "Código de isenção",
    "total": "Total da linha (sem imposto)",
}


@dataclass
class AssistantChoices:
    documents_table: TableRef
    lines_table: TableRef
    header_columns: dict[str, str]          # campo canónico -> coluna
    header_defaults: dict[str, str]         # campo canónico -> valor fixo
    line_columns: dict[str, str]
    line_defaults: dict[str, str]
    cursor_column: str
    cursor_type: str
    link_column: str                        # coluna das linhas que aponta para o documento
    batch_size: int = 200


def build_mapping(db: HostDatabase, choices: AssistantChoices, document_columns: set[str],
                  line_columns: set[str]) -> dict:
    header = {k: v for k, v in choices.header_columns.items() if v}
    lines = {k: v for k, v in choices.line_columns.items() if v}
    documents_query = build_select(
        db, choices.documents_table, list(header.values()), choices.cursor_column, ">", "cursor",
        choices.cursor_column, document_columns,
    )
    lines_query = build_select(
        db, choices.lines_table, list(lines.values()), choices.link_column, "=", "document_id",
        lines.get("line_number"), line_columns,
    )
    mapping = {
        "documents_query": documents_query,
        "lines_query": lines_query,
        "cursor_column": choices.cursor_column,
        "cursor_type": choices.cursor_type,
        "initial_cursor": "0" if choices.cursor_type == "int" else ("1900-01-01T00:00:00" if choices.cursor_type == "datetime" else ""),
        "batch_size": choices.batch_size,
        "fields": header,
        "line_fields": lines,
        "defaults": {k: v for k, v in choices.header_defaults.items() if v and k not in header},
        "line_defaults": {k: v for k, v in choices.line_defaults.items() if v and k not in lines},
        "assistant": {
            "documents_table": choices.documents_table.qualified,
            "lines_table": choices.lines_table.qualified,
            "link_column": choices.link_column,
        },
    }
    return mapping


@dataclass
class PreviewItem:
    document_id: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def preview(db: HostDatabase, mapping: dict, limit: int = 3) -> list[PreviewItem]:
    """Lê os primeiros documentos e mostra como ficariam, SEM importar nada."""
    problems = validate_mapping(mapping)
    if problems:
        return [PreviewItem("—", False, problems)]
    cursor = cursor_value(mapping, "")
    rows = db.fetch_all(mapping["documents_query"], {"cursor": cursor}, max_rows=limit)
    items = []
    id_column = mapping["fields"]["source_document_id"]
    for row in rows:
        try:
            document_id = row_value(row, id_column)
            lines = db.fetch_all(mapping["lines_query"], {"document_id": document_id}, max_rows=200)
            doc = parse_document(rows_to_payload(mapping, row, lines))
        except KeyError as exc:
            items.append(PreviewItem("?", False, [f"Coluna {exc.args[0]!r} não existe no resultado."]))
            continue
        except CanonicalError as exc:
            items.append(PreviewItem(str(row_value(row, id_column)), False, exc.errors))
            continue
        errors = validate_document(doc)
        items.append(PreviewItem(
            doc.source_document_id, not errors, errors,
            {"número": doc.document_number, "data": doc.document_date, "cliente": doc.customer_name,
             "total": doc.total, "linhas": len(doc.lines)},
        ))
    return items
