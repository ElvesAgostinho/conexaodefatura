"""Sugestão automática de mapeamento (bases SQLite FICTÍCIAS, nenhum sistema real)."""

import sqlite3
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from host_connector.catalog import table_columns
from host_connector.config import HostConnectionConfig
from host_connector.connection import HostDatabase
from sources.assistant import AssistantChoices, build_mapping, preview
from sources.autodetect import match_columns, normalize, suggest, HEADER_SYNONYMS, LINE_SYNONYMS

WITH_FK = """
CREATE TABLE Clientes (Id INTEGER PRIMARY KEY, Nome TEXT);
CREATE TABLE Faturas (IdFatura INTEGER PRIMARY KEY, TipoDocumento TEXT, Serie TEXT, NumeroDocumento TEXT,
                      DataEmissao TEXT, NomeCliente TEXT, NifCliente TEXT, ValorLiquido NUMERIC, TotalImposto NUMERIC,
                      TotalGeral NUMERIC);
CREATE TABLE ItensFatura (IdItem INTEGER PRIMARY KEY, FaturaRef INTEGER REFERENCES Faturas(IdFatura),
                          Linha INTEGER, Artigo TEXT, Designacao TEXT, Qtd NUMERIC, Preco NUMERIC, TaxaIVA NUMERIC,
                          ValorIVA NUMERIC, TotalLinha NUMERIC, MotivoIsencao TEXT);
INSERT INTO Faturas VALUES (1, 'FT', 'A', 'FT A/1', '2026-01-05', 'Cliente Um', '5000000001', 100, 14, 114);
INSERT INTO ItensFatura VALUES (1, 1, 1, 'X1', 'Produto', 2, 50, 14, 14, 100, NULL);
"""

ENGLISH_NO_FK = """
CREATE TABLE invoice_header (invoice_id INTEGER PRIMARY KEY, doc_type TEXT, series TEXT, invoice_number TEXT,
                             invoice_date TEXT, customer_name TEXT, tax_id TEXT, currency TEXT, net_amount NUMERIC,
                             tax_amount NUMERIC, gross_total NUMERIC);
CREATE TABLE invoice_lines (line_id INTEGER PRIMARY KEY, invoice_id INTEGER, line_number INTEGER, sku TEXT,
                            description TEXT, quantity NUMERIC, unit_price NUMERIC, discount NUMERIC, tax_rate NUMERIC,
                            tax NUMERIC, line_total NUMERIC);
"""

NOT_INVOICES = """
CREATE TABLE aluno (nome TEXT, nota INTEGER);
CREATE TABLE turma (id INTEGER PRIMARY KEY, sala TEXT);
"""


def fake_db(test, script):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = str(Path(tmp.name) / "fake.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.commit()
    conn.close()
    db = HostDatabase(HostConnectionConfig.from_values(engine="sqlite", name=path))
    test.addCleanup(db.dispose)
    return db


class MatchTests(SimpleTestCase):
    def test_normalize(self):
        self.assertEqual(normalize("Descrição_do Artigo"), "descricaodoartigo")
        self.assertEqual(normalize("NIF-Cliente"), "nifcliente")

    def test_each_column_used_once_and_best_match_wins(self):
        chosen = match_columns(["Id", "Total", "TotalIVA", "Data", "Nome"], HEADER_SYNONYMS)
        self.assertEqual(chosen["total"], "Total")
        self.assertEqual(chosen["tax_amount"], "TotalIVA")
        self.assertEqual(chosen["document_date"], "Data")
        self.assertEqual(len(set(chosen.values())), len(chosen))

    def test_desconto_not_confused_with_descricao(self):
        chosen = match_columns(["Descricao", "Desconto"], LINE_SYNONYMS)
        self.assertEqual((chosen["description"], chosen["discount"]), ("Descricao", "Desconto"))

    def test_no_match(self):
        self.assertEqual(match_columns(["xpto", "abc"], HEADER_SYNONYMS), {})


class SuggestTests(TestCase):
    def test_portuguese_with_foreign_key(self):
        db = fake_db(self, WITH_FK)
        s = suggest(db)
        self.assertTrue(s.complete, s.notes)
        self.assertEqual((s.documents_table.name, s.lines_table.name, s.link_column), ("Faturas", "ItensFatura", "FaturaRef"))
        self.assertEqual((s.cursor_column, s.cursor_type), ("IdFatura", "int"))
        self.assertEqual(s.header["document_number"], "NumeroDocumento")
        self.assertEqual(s.header["customer_nif"], "NifCliente")
        self.assertEqual(s.header["total"], "TotalGeral")
        self.assertEqual(s.lines["description"], "Designacao")
        self.assertEqual(s.lines["tax_exemption_code"], "MotivoIsencao")
        self.assertNotIn("IdItem", s.lines.values())  # a chave das linhas não é um campo
        self.assertTrue(any("chave estrangeira" in n for n in s.notes))
        self.assertEqual(s.initial()["hd_currency"], "AOA")  # sem coluna de moeda

        # A sugestão gera um mapeamento que funciona de ponta a ponta.
        def cols(table):
            return {c["name"] for c in table_columns(db, table)}

        mapping = build_mapping(db, AssistantChoices(
            documents_table=s.documents_table, lines_table=s.lines_table, header_columns=s.header,
            header_defaults={"currency": "AOA"}, line_columns=s.lines, line_defaults={},
            cursor_column=s.cursor_column, cursor_type=s.cursor_type, link_column=s.link_column,
        ), cols(s.documents_table), cols(s.lines_table))
        items = preview(db, mapping)
        self.assertEqual([i.ok for i in items], [True], [i.errors for i in items])

    def test_english_without_foreign_key(self):
        s = suggest(fake_db(self, ENGLISH_NO_FK))
        self.assertTrue(s.complete, s.notes)
        self.assertEqual((s.documents_table.name, s.lines_table.name, s.link_column),
                         ("invoice_header", "invoice_lines", "invoice_id"))
        self.assertEqual(s.header["customer_nif"], "tax_id")
        self.assertEqual(s.header["subtotal"], "net_amount")
        self.assertEqual(s.lines["product_code"], "sku")
        self.assertEqual(s.lines["tax_amount"], "tax")

    def test_database_without_invoices(self):
        s = suggest(fake_db(self, NOT_INVOICES))
        self.assertFalse(s.complete)
        self.assertIsNone(s.documents_table)
        self.assertTrue(any("manualmente" in n for n in s.notes))
