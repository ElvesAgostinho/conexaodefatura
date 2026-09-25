"""Mapeamento configurável: linhas de uma BD de origem -> documento canónico.

Não há estrutura de BD assumida para nenhum sistema: as consultas e as colunas são
configuradas por origem, depois de estudada a BD real (para o HOST, na Fase 6, com
host_inspect/host_sample). Formato (JSON):

{
  "documents_query": "SELECT ... FROM ... WHERE <coluna> > :cursor ORDER BY <coluna>",
  "lines_query":     "SELECT ... FROM ... WHERE <coluna_doc> = :document_id",
  "cursor_column":   "<coluna do documents_query que avança o cursor>",
  "cursor_type":     "int" | "str" | "datetime",          (omissão: int)
  "initial_cursor":  "0",
  "batch_size":      200,
  "fields":        { "<campo canónico>": "<coluna do documents_query>", ... },
  "line_fields":   { "<campo canónico da linha>": "<coluna do lines_query>", ... },
  "defaults":      { "<campo canónico>": <valor fixo quando não há coluna> },
  "line_defaults": { "<campo canónico da linha>": <valor fixo> }
}

As consultas passam pela guarda de SQL (só leitura) e usam só parâmetros
(:cursor, :document_id), nunca concatenação.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from host_connector.sql_guard import ReadOnlyViolation, assert_read_only
from invoices.canonical import HEADER_FIELDS, LINE_FIELDS, REQUIRED_HEADER_FIELDS, REQUIRED_LINE_FIELDS

CURSOR_TYPES = ("int", "str", "datetime")
MAX_BATCH = 1000
_KEYS = {"documents_query", "lines_query", "cursor_column", "cursor_type", "initial_cursor", "batch_size", "assistant",
         "filters",
         "fields", "line_fields", "defaults", "line_defaults"}


FILTER_TYPE_RE = re.compile(r"^[A-Za-z0-9/ .-]{1,20}$")
MAX_FILTER_TYPES = 50


def filter_params(mapping: dict) -> dict:
    """Parâmetros dos filtros ("começar a partir de" e tipos de documento)."""
    filters = mapping.get("filters") or {}
    params = {}
    if filters.get("start_date"):
        params["start_date"] = date.fromisoformat(filters["start_date"])
    for i, value in enumerate(filters.get("document_types") or []):
        params[f"doc_type_{i}"] = value
    return params


def query_params(mapping: dict, cursor, engine: str = "") -> dict:
    """Parâmetros da consulta de documentos: cursor + filtros."""
    params = {"cursor": cursor, **filter_params(mapping)}
    if engine == "sqlite" and "start_date" in params:
        params["start_date"] = params["start_date"].isoformat()  # datas guardadas como texto
    return params


def _validate_filters(mapping: dict) -> list[str]:
    filters = mapping.get("filters")
    if filters in (None, {}):
        return []
    if not isinstance(filters, dict) or set(filters) - {"start_date", "document_types"}:
        return ["filters: só são aceites start_date e document_types."]
    errors = []
    query = mapping.get("documents_query") or ""
    if filters.get("start_date"):
        try:
            date.fromisoformat(str(filters["start_date"]))
        except ValueError:
            errors.append("filters.start_date: data inválida (AAAA-MM-DD).")
        if ":start_date" not in query:
            errors.append("documents_query: tem de usar :start_date (filtro por data).")
    types = filters.get("document_types") or []
    if not isinstance(types, list) or len(types) > MAX_FILTER_TYPES:
        errors.append(f"filters.document_types: lista com até {MAX_FILTER_TYPES} tipos.")
    else:
        for i, value in enumerate(types):
            if not isinstance(value, str) or not FILTER_TYPE_RE.match(value):
                errors.append(f"filters.document_types: tipo inválido ({value!r}).")
            elif f":doc_type_{i}" not in query:
                errors.append(f"documents_query: tem de usar :doc_type_{i} (filtro por tipo).")
    return errors


def validate_mapping(mapping: Any) -> list[str]:
    if not isinstance(mapping, dict):
        return ["O mapeamento tem de ser um objeto JSON."]
    errors: list[str] = []
    unknown = sorted(set(mapping) - _KEYS)
    if unknown:
        errors.append(f"Chaves desconhecidas: {', '.join(unknown)}.")

    for key, param in (("documents_query", ":cursor"), ("lines_query", ":document_id")):
        sql = mapping.get(key)
        if not isinstance(sql, str) or not sql.strip():
            errors.append(f"{key}: obrigatório.")
            continue
        try:
            assert_read_only(sql)
        except ReadOnlyViolation as exc:
            errors.append(f"{key}: {exc}")
        if param not in sql:
            errors.append(f"{key}: tem de usar o parâmetro {param}.")

    if not isinstance(mapping.get("cursor_column"), str) or not mapping.get("cursor_column", "").strip():
        errors.append("cursor_column: obrigatório.")
    if mapping.get("cursor_type", "int") not in CURSOR_TYPES:
        errors.append(f"cursor_type: um de {', '.join(CURSOR_TYPES)}.")
    batch = mapping.get("batch_size", 200)
    if not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= MAX_BATCH:
        errors.append(f"batch_size: inteiro entre 1 e {MAX_BATCH}.")
    try:
        cursor_value(mapping, mapping.get("initial_cursor"))
    except ValueError as exc:
        errors.append(f"initial_cursor: {exc}")

    errors += _validate_filters(mapping)
    if not isinstance(mapping.get("fields", {}), dict) or "source_document_id" not in mapping.get("fields", {}):
        errors.append("fields.source_document_id: tem de vir de uma coluna (é usado em :document_id).")
    errors += _check_fields(mapping, "fields", "defaults", HEADER_FIELDS, REQUIRED_HEADER_FIELDS)
    errors += _check_fields(mapping, "line_fields", "line_defaults", LINE_FIELDS, REQUIRED_LINE_FIELDS)
    return errors


def _check_fields(mapping, fields_key, defaults_key, allowed, required) -> list[str]:
    errors = []
    fields = mapping.get(fields_key, {})
    defaults = mapping.get(defaults_key, {})
    if not isinstance(fields, dict) or not isinstance(defaults, dict):
        return [f"{fields_key} e {defaults_key} têm de ser objetos."]
    for key in (fields_key, defaults_key):
        bad = sorted(set(mapping.get(key, {})) - set(allowed))
        if bad:
            errors.append(f"{key}: campos canónicos desconhecidos: {', '.join(bad)}.")
    for name, column in fields.items():
        if not isinstance(column, str) or not column.strip():
            errors.append(f"{fields_key}.{name}: nome de coluna inválido.")
    missing = [name for name in required if name not in fields and name not in defaults]
    if missing:
        errors.append(f"{fields_key}: faltam campos obrigatórios: {', '.join(missing)}.")
    return errors


def cursor_value(mapping: dict, raw: Any):
    """Converte o cursor guardado (texto) para o tipo usado na consulta."""
    kind = mapping.get("cursor_type", "int")
    if raw is None or raw == "":
        raw = mapping.get("initial_cursor", "0" if kind == "int" else "")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"valor inteiro esperado ({raw!r}).") from None
    if kind == "datetime":
        if isinstance(raw, datetime):
            return raw
        try:
            return datetime.fromisoformat(str(raw) or "1900-01-01")
        except ValueError:
            raise ValueError(f"data/hora ISO esperada ({raw!r}).") from None
    return str(raw)


def cursor_to_text(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _get(row: dict, column: str):
    """Leitura de coluna sem distinguir maiúsculas (Oracle devolve nomes em maiúsculas)."""
    if column in row:
        return row[column]
    lowered = column.lower()
    for key, value in row.items():
        if str(key).lower() == lowered:
            return value
    raise KeyError(column)


def row_value(row: dict, column: str):
    return _get(row, column)


def rows_to_payload(mapping: dict, header: dict, lines: list[dict]) -> dict:
    """Constrói o dict canónico. Colunas mapeadas em falta no resultado -> KeyError."""
    payload = dict(mapping.get("defaults", {}))
    for name, column in mapping.get("fields", {}).items():
        payload[name] = _get(header, column)
    payload["lines"] = []
    for index, row in enumerate(lines, start=1):
        line = dict(mapping.get("line_defaults", {}))
        for name, column in mapping.get("line_fields", {}).items():
            line[name] = _get(row, column)
        line.setdefault("line_number", index)
        payload["lines"].append(line)
    return payload
