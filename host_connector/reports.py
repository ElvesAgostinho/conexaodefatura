"""Grava o relatório de estrutura da BD do HOST em JSON (máquina) e Markdown (leitura)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROLE_LABELS = {
    "cabecalho_fatura": "Cabeçalho da fatura",
    "linhas_fatura": "Linhas da fatura",
    "cliente": "Cliente",
    "artigos": "Artigos / produtos",
    "impostos": "Impostos",
    "pagamentos": "Pagamentos",
    "series": "Séries",
    "anulacoes_notas_credito": "Anulações / notas de crédito",
}


def write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    database = str(report["connection"].get("database") or "host").replace("/", "_").replace("\\", "_")
    base = output_dir / f"host_schema_{Path(database).stem}_{stamp}"

    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    md_path = base.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _cell(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    conn = report["connection"]
    summary = report["summary"]
    lines = [
        "# Estrutura da base de dados do HOST",
        "",
        f"- Gerado em: {report['generated_at']}",
        f"- Motor: {report['dialect']} {report.get('server_version') or ''}".rstrip(),
        f"- Servidor: {conn.get('host') or '-'}:{conn.get('port') or '-'}",
        f"- Base de dados: {conn.get('database')}",
        f"- Schema por omissão: {report.get('default_schema')}",
        f"- Schemas: {summary['schemas']} · Tabelas: {summary['tables']} · Vistas: {summary['views']}",
        "",
    ]

    if report.get("databases"):
        lines += ["## Bases de dados no servidor", "", ", ".join(report["databases"]), ""]

    if report.get("warnings"):
        lines += ["## Avisos", ""] + [f"- {w}" for w in report["warnings"]] + [""]

    lines += [
        "## Candidatos (sugestão automática, CONFIRMAR MANUALMENTE)",
        "",
        "Ordenados por semelhança de nomes. Isto não é o mapeamento: é um ponto de partida para a Fase 6.",
        "",
    ]
    for role, items in report["candidates"].items():
        lines.append(f"### {ROLE_LABELS.get(role, role)}")
        lines.append("")
        if not items:
            lines += ["_Nenhum candidato por nome._", ""]
            continue
        lines += ["| Objeto | Tipo | Linhas (est.) | Palavras na tabela | Palavras nas colunas |", "|---|---|---|---|---|"]
        for item in items:
            lines.append(
                f"| {_cell(item['object'])} | {item['kind']} | {_cell(item['row_estimate'])} | "
                f"{_cell(', '.join(item['matched_table_keywords']))} | {_cell(', '.join(item['matched_column_keywords']))} |"
            )
        lines.append("")

    lines += ["## Índice de objetos", "", "| Objeto | Tipo | Linhas (est.) | Colunas | PK |", "|---|---|---|---|---|"]
    for schema in report["schemas"]:
        for obj in (*schema["tables"], *schema["views"]):
            lines.append(
                f"| {_cell(obj['schema'])}.{_cell(obj['name'])} | {obj['kind']} | {_cell(obj['row_estimate'])} | "
                f"{len(obj['columns'])} | {_cell(', '.join(obj['primary_key']))} |"
            )
    lines.append("")

    lines += ["## Detalhe", ""]
    for schema in report["schemas"]:
        for obj in (*schema["tables"], *schema["views"]):
            lines.append(f"### {obj['schema']}.{obj['name']} ({obj['kind']})")
            lines.append("")
            if obj.get("comment"):
                lines += [f"_{obj['comment']}_", ""]
            if obj.get("row_estimate") is not None:
                lines += [f"Linhas (estimativa): {obj['row_estimate']}", ""]
            pk = set(obj["primary_key"])
            lines += ["| Coluna | Tipo | Nulo | PK | Default |", "|---|---|---|---|---|"]
            for col in obj["columns"]:
                lines.append(
                    f"| {_cell(col['name'])} | {_cell(col['type'])} | {'sim' if col['nullable'] else 'não'} | "
                    f"{'sim' if col['name'] in pk else ''} | {_cell(col['default'])} |"
                )
            lines.append("")
            if obj["foreign_keys"]:
                lines.append("Chaves estrangeiras:")
                for fk in obj["foreign_keys"]:
                    lines.append(
                        f"- ({', '.join(fk['columns'])}) → {fk['referred_schema']}.{fk['referred_table']}"
                        f"({', '.join(fk['referred_columns'])})"
                    )
                lines.append("")
            if obj["indexes"]:
                lines.append("Índices:")
                for ix in obj["indexes"]:
                    unique = " UNIQUE" if ix["unique"] else ""
                    lines.append(f"- {ix['name']}{unique}: {', '.join(c for c in ix['columns'] if c)}")
                lines.append("")
    return "\n".join(lines)
