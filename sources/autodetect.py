"""Sugestão automática de mapeamento a partir da estrutura REAL da base de dados.

Não assume a estrutura de nenhum sistema: compara nomes de tabelas e colunas com
sinónimos comuns (português e inglês) e usa as chaves primárias e estrangeiras. O
resultado só pré-preenche o assistente; o cliente confirma na pré-visualização.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy import inspect

from host_connector.catalog import TableRef, list_tables
from host_connector.connection import HostDatabase

# Por ordem de preferência. Nomes normalizados (minúsculas, sem acentos nem separadores).
HEADER_SYNONYMS = {
    "source_document_id": ["id", "iddocumento", "documentoid", "iddoc", "docid", "idfatura", "faturaid", "idfactura",
                           "facturaid", "invoiceid", "idcabecalho", "cabecalhoid", "stamp"],
    "document_type": ["tipodoc", "tipodocumento", "doctype", "documenttype", "tipofatura", "tipofactura", "codtipo",
                      "tipo"],
    "series": ["serie", "series", "codserie", "seriedoc", "seriedocumento"],
    "document_number": ["numdoc", "numerodoc", "numerodocumento", "nrdoc", "ndoc", "docnumber", "invoicenumber",
                        "numfatura", "numfactura", "numero", "number", "invoiceno"],
    "document_date": ["datadoc", "datadocumento", "dataemissao", "docdate", "invoicedate", "datafatura",
                      "datafactura", "data", "date"],
    "customer_name": ["clientenome", "nomecliente", "customername", "nomeadquirente", "cliente", "customer",
                      "entidade", "nome", "name"],
    "customer_nif": ["clientenif", "nifcliente", "nif", "ncontribuinte", "numcontribuinte", "contribuinte", "taxid",
                     "customertaxid", "vatnumber", "nuit"],
    "currency": ["moeda", "codmoeda", "currency", "currencycode"],
    "subtotal": ["totalliquido", "subtotal", "valorliquido", "totalsemiva", "totalsemimposto", "basetributavel",
                 "totalbase", "netamount", "nettotal", "base"],
    "tax_amount": ["totaliva", "totalimposto", "valoriva", "valorimposto", "taxamount", "totaltax", "iva", "imposto",
                   "tax", "vat"],
    "total": ["totaldocumento", "totalfatura", "totalfactura", "totalgeral", "valortotal", "totalcomiva",
              "grosstotal", "total", "amount"],
    "document_hash": ["hash", "hashdocumento", "hashdoc", "hashfatura", "hashfactura", "assinatura",
                      "assinaturadigital", "signature", "docsignature"],
    "hash_control": ["hashcontrol", "hashcontrolo", "controlohash", "versaochave", "versaohash", "keyversion",
                     "hashversion"],
}
LINE_SYNONYMS = {
    "line_number": ["numlinha", "nlinha", "linha", "numerolinha", "linenumber", "lineno", "ordem", "ord", "seq"],
    "product_code": ["codartigo", "codigoartigo", "codproduto", "codigoproduto", "productcode", "artigo", "sku",
                     "referencia", "ref", "codigo"],
    "description": ["descricao", "designacao", "descr", "description", "nomeartigo", "produto", "product"],
    "quantity": ["quantidade", "qtd", "qtde", "quantity", "qty"],
    "unit_price": ["precounitario", "precounit", "valorunitario", "unitprice", "preco", "price", "pu"],
    "discount": ["desconto", "valordesconto", "discount", "discountamount"],
    "tax_rate": ["taxaiva", "taxaimposto", "percentagemiva", "ivapercent", "taxrate", "taxa", "percentiva"],
    "tax_amount": ["valoriva", "valorimposto", "taxamount", "iva", "imposto", "tax"],
    "tax_exemption_code": ["codisencao", "codigoisencao", "motivoisencao", "isencao", "taxexemptioncode",
                           "exemptioncode"],
    "total": ["totallinha", "valortotal", "linetotal", "total", "valor", "amount"],
}
HEADER_TABLE_WORDS = ("fatura", "factura", "invoice", "documento", "document", "cabecalho", "header", "venda", "sale")
LINE_TABLE_WORDS = ("linha", "line", "detalhe", "detail", "item", "artigo")


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def match_columns(columns: list[str], synonyms: dict[str, list[str]]) -> dict[str, str]:
    """Campo canónico -> coluna. Cada coluna é usada no máximo uma vez."""
    normalized = {c: normalize(c) for c in columns}
    candidates = []
    for canonical, words in synonyms.items():
        for rank, word in enumerate(words):
            for column, norm in normalized.items():
                if norm == word:
                    score = 100 - rank
                elif len(word) >= 4 and (norm.startswith(word) or norm.endswith(word)):
                    score = 40 - rank
                else:
                    continue
                candidates.append((score, canonical, column))
    chosen: dict[str, str] = {}
    used: set[str] = set()
    for score, canonical, column in sorted(candidates, key=lambda c: -c[0]):
        if canonical not in chosen and column not in used:
            chosen[canonical] = column
            used.add(column)
    return chosen


@dataclass
class Suggestion:
    documents_table: TableRef | None = None
    lines_table: TableRef | None = None
    header: dict[str, str] = field(default_factory=dict)
    lines: dict[str, str] = field(default_factory=dict)
    link_column: str = ""
    cursor_column: str = ""
    cursor_type: str = "int"
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.documents_table and self.lines_table and self.link_column and self.cursor_column
                    and "source_document_id" in self.header)

    def initial(self) -> dict:
        """Valores iniciais para o formulário do assistente."""
        data = {"cursor_column": self.cursor_column, "cursor_type": self.cursor_type, "link_column": self.link_column,
                "batch_size": 200}
        data.update({f"h_{k}": v for k, v in self.header.items()})
        data.update({f"l_{k}": v for k, v in self.lines.items()})
        if "currency" not in self.header:
            data["hd_currency"] = "AOA"
        return data


def _table_score(ref: TableRef, columns: list[str], words, synonyms) -> int:
    name = normalize(ref.name)
    score = 3 if any(w in name for w in words) else 0
    return score + len(match_columns(columns, synonyms))


def suggest(db: HostDatabase, tables: list[TableRef] | None = None, max_tables: int = 400) -> Suggestion:
    tables = (tables if tables is not None else list_tables(db))[:max_tables]
    suggestion = Suggestion()
    with db.connect() as conn:
        inspector = inspect(conn)
        default = inspector.default_schema_name
        info = {}
        for ref in tables:
            schema = None if ref.schema in (None, default) else ref.schema
            try:
                columns = inspector.get_columns(ref.name, schema=schema)
            except Exception:  # noqa: BLE001 - vista sem acesso: ignora
                continue
            try:
                pk = inspector.get_pk_constraint(ref.name, schema=schema).get("constrained_columns") or []
                fks = inspector.get_foreign_keys(ref.name, schema=schema) if ref.kind == "tabela" else []
            except Exception:  # noqa: BLE001
                pk, fks = [], []
            info[ref.qualified] = {"ref": ref, "columns": columns, "names": [c["name"] for c in columns],
                                   "pk": pk, "fks": fks}

    if not info:
        suggestion.notes.append("Não foram encontradas tabelas acessíveis.")
        return suggestion

    header_scores = sorted(
        ((_table_score(t["ref"], t["names"], HEADER_TABLE_WORDS, HEADER_SYNONYMS), q) for q, t in info.items()),
        key=lambda s: -s[0])
    best_header_score, header_q = header_scores[0]
    if best_header_score < 4:
        suggestion.notes.append("Não foi encontrada uma tabela que pareça de faturas. Escolha-a manualmente.")
        return suggestion
    header = info[header_q]

    # Linhas: de preferência a tabela com chave estrangeira para o cabeçalho.
    lines_q, link = None, ""
    for q, t in info.items():
        if q == header_q:
            continue
        for fk in t["fks"]:
            if fk.get("referred_table", "").lower() == header["ref"].name.lower() and fk.get("constrained_columns"):
                lines_q, link = q, fk["constrained_columns"][0]
                suggestion.notes.append(f"Ligação entre tabelas encontrada pela chave estrangeira {q}.{link}.")
                break
        if lines_q:
            break
    if not lines_q:
        line_scores = sorted(
            ((_table_score(t["ref"], t["names"], LINE_TABLE_WORDS, LINE_SYNONYMS), q)
             for q, t in info.items() if q != header_q), key=lambda s: -s[0])
        if line_scores and line_scores[0][0] >= 4:
            lines_q = line_scores[0][1]
    if not lines_q:
        suggestion.notes.append("Não foi encontrada a tabela das linhas. Escolha-a manualmente.")
        return suggestion
    lines = info[lines_q]

    suggestion.documents_table, suggestion.lines_table = header["ref"], lines["ref"]
    suggestion.header = match_columns(header["names"], HEADER_SYNONYMS)
    if not link:
        base = normalize(header["ref"].name).rstrip("s")
        wanted = [f"{base}id", f"id{base}", "documentoid", "iddocumento", "docid", "iddoc", "faturaid", "idfatura",
                  "cabecalhoid", "headerid", "invoiceid"]
        for column in lines["names"]:
            if normalize(column) in wanted:
                link = column
                break
    suggestion.link_column = link
    line_columns = [c for c in lines["names"] if c != link and c not in lines["pk"]]
    suggestion.lines = match_columns(line_columns, LINE_SYNONYMS)

    # Cursor: chave primária numérica única; senão uma data/hora de criação.
    pk = header["pk"]
    types = {c["name"]: str(c["type"]).upper() for c in header["columns"]}
    if len(pk) == 1 and any(t in types.get(pk[0], "") for t in ("INT", "NUMERIC", "DECIMAL", "NUMBER")):
        suggestion.cursor_column, suggestion.cursor_type = pk[0], "int"
        suggestion.header.setdefault("source_document_id", pk[0])
    else:
        for column in header["names"]:
            if normalize(column) in ("datacriacao", "createdat", "datahora", "dataregisto", "datainsercao"):
                suggestion.cursor_column, suggestion.cursor_type = column, "datetime"
                break
    if not suggestion.cursor_column:
        suggestion.notes.append("Não foi encontrada uma coluna crescente para ler só documentos novos.")
    return suggestion
