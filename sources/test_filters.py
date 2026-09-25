"""'Começar a partir de' e filtro por tipo de documento (BD de origem FICTÍCIA em SQLite)."""

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from companies.models import Company
from host_connector.catalog import CatalogError, TableRef, build_select, distinct_values, table_columns
from host_connector.config import HostConnectionConfig
from host_connector.connection import HostDatabase
from invoices.models import Invoice
from sources.assistant import AssistantChoices, build_mapping, preview
from sources.mapping import query_params, validate_mapping
from sources.models import DataSource
from sources.sync import sync_source

FAKE = """
CREATE TABLE Docs (Id INTEGER PRIMARY KEY, Tipo TEXT, Serie TEXT, Numero TEXT, Data TEXT, Cliente TEXT,
                   Base NUMERIC, Iva NUMERIC, Total NUMERIC);
CREATE TABLE Linhas (DocId INTEGER, Ord INTEGER, Descr TEXT, Qtd NUMERIC, Preco NUMERIC, Taxa NUMERIC,
                     Imposto NUMERIC, Valor NUMERIC);
"""
ROWS = [  # (id, tipo, data)
    (1, "FT", "2024-12-15"), (2, "PP", "2025-01-05"), (3, "FT", "2025-01-10"), (4, "FR", "2025-02-01"),
    (5, "OR", "2025-02-02"), (6, "NC", "2025-03-01"), (7, "FT", "2025-03-05"),
]


def make_db(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = str(Path(tmp.name) / "origem.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(FAKE)
    for doc_id, tipo, data in ROWS:
        conn.execute("INSERT INTO Docs VALUES (?, ?, 'A', ?, ?, 'Cliente', 100, 14, 114)",
                     (doc_id, tipo, f"{tipo} A/{doc_id}", data))
        conn.execute("INSERT INTO Linhas VALUES (?, 1, 'Artigo', 2, 50, 14, 14, 100)", (doc_id,))
    conn.commit()
    conn.close()
    db = HostDatabase(HostConnectionConfig.from_values(engine="sqlite", name=path))
    test.addCleanup(db.dispose)
    return path, db


def choices(**extra):
    data = dict(
        documents_table=TableRef(None, "Docs"), lines_table=TableRef(None, "Linhas"),
        header_columns={"source_document_id": "Id", "document_type": "Tipo", "series": "Serie",
                        "document_number": "Numero", "document_date": "Data", "customer_name": "Cliente",
                        "subtotal": "Base", "tax_amount": "Iva", "total": "Total"},
        header_defaults={"currency": "AOA"},
        line_columns={"line_number": "Ord", "description": "Descr", "quantity": "Qtd", "unit_price": "Preco",
                      "tax_rate": "Taxa", "tax_amount": "Imposto", "total": "Valor"},
        line_defaults={}, cursor_column="Id", cursor_type="int", link_column="DocId",
    )
    data.update(extra)
    return AssistantChoices(**data)


class FilterSqlTests(TestCase):
    def setUp(self):
        self.path, self.db = make_db(self)
        self.cols = {c["name"] for c in table_columns(self.db, TableRef(None, "Docs"))}
        self.line_cols = {c["name"] for c in table_columns(self.db, TableRef(None, "Linhas"))}

    def mapping(self, **extra):
        return build_mapping(self.db, choices(**extra), self.cols, self.line_cols)

    def test_distinct_types_with_counts(self):
        values = dict(distinct_values(self.db, TableRef(None, "Docs"), "Tipo"))
        self.assertEqual(values, {"FT": 3, "PP": 1, "FR": 1, "OR": 1, "NC": 1})
        self.assertEqual(distinct_values(self.db, TableRef(None, "Docs"), "Tipo")[0], ("FT", 3))  # mais comum primeiro
        with self.assertRaises(CatalogError):
            distinct_values(self.db, TableRef(None, "Docs"), "NaoExiste")

    def test_generated_sql_uses_parameters(self):
        m = self.mapping(start_date=date(2025, 1, 1), document_types=["FT", "FR", "NC"])
        self.assertEqual(validate_mapping(m), [])
        q = m["documents_query"]
        self.assertIn('"Data" >= :start_date', q)
        self.assertIn('"Tipo" IN (:doc_type_0, :doc_type_1, :doc_type_2)', q)
        self.assertNotIn("FT", q)  # valores nunca no SQL
        self.assertEqual(m["filters"], {"start_date": "2025-01-01", "document_types": ["FT", "FR", "NC"]})
        params = query_params(m, 0, "sqlite")
        self.assertEqual((params["start_date"], params["doc_type_1"]), ("2025-01-01", "FR"))
        self.assertEqual(query_params(m, 0, "mssql")["start_date"], date(2025, 1, 1))

    def test_preview_respects_filters(self):
        items = preview(self.db, self.mapping(start_date=date(2025, 1, 1), document_types=["FT", "FR", "NC"]),
                        limit=10)
        self.assertEqual([i.document_id for i in items], ["3", "4", "6", "7"])  # sem 2024, PP e OR

    def test_initial_cursor(self):
        m = self.mapping(initial_cursor="4")
        self.assertEqual([i.document_id for i in preview(self.db, m, limit=10)], ["5", "6", "7"])

    def test_no_filters_keeps_old_behaviour(self):
        m = self.mapping()
        self.assertEqual(m["filters"], {})
        self.assertNotIn(":start_date", m["documents_query"])
        self.assertEqual(len(preview(self.db, m, limit=10)), 7)

    def test_filter_ignored_when_field_comes_from_fixed_value(self):
        header = dict(choices().header_columns)
        header.pop("document_type")
        m = build_mapping(self.db, choices(header_columns=header, header_defaults={"document_type": "FT"},
                                           document_types=["FT"]), self.cols, self.line_cols)
        self.assertNotIn("document_types", m["filters"])

    def test_injection_in_filter_definition_is_refused(self):
        ref = TableRef(None, "Docs")
        for conditions in ([("Tipo; DROP TABLE Docs", "IN", ["doc_type_0"])], [("Tipo", "LIKE", ["x"])],
                           [("Tipo", "IN", ["x); DROP"])], [("Tipo", "IN", [])]):
            with self.subTest(conditions=conditions), self.assertRaises(CatalogError):
                build_select(self.db, ref, ["Id"], "Id", ">", "cursor", None, self.cols, conditions)

    def test_sync_imports_only_filtered(self):
        company = Company.objects.create(name="Hotel", nif="5000000000")
        source = DataSource.objects.create(
            company=company, code="ERP", name="ERP", kind="DATABASE", db_engine="sqlite", db_name=self.path,
            mapping=self.mapping(start_date=date(2025, 1, 1), document_types=["FT", "FR", "NC"]))
        result = sync_source(source)
        self.assertEqual(result.created, 4)
        self.assertEqual(sorted(Invoice.objects.values_list("document_type", flat=True)), ["FR", "FT", "FT", "NC"])
        self.assertEqual(result.cursor, "7")
        self.assertEqual(sync_source(source).created, 0)


class FilterValidationTests(SimpleTestCase):
    BASE = {"documents_query": "SELECT * FROM D WHERE Id > :cursor", "lines_query": "SELECT * FROM L WHERE d = :document_id",
            "cursor_column": "Id", "fields": {"source_document_id": "Id", "document_type": "T", "series": "S",
                                              "document_number": "N", "document_date": "D", "customer_name": "C",
                                              "subtotal": "B", "tax_amount": "I", "total": "T2"},
            "line_fields": {"description": "d", "quantity": "q", "unit_price": "p", "tax_rate": "r",
                            "tax_amount": "a", "total": "t"}}

    def errors(self, filters, query=None):
        m = dict(self.BASE, filters=filters)
        if query:
            m["documents_query"] = query
        return " ".join(validate_mapping(m))

    def test_valid_and_invalid(self):
        q = "SELECT * FROM D WHERE Id > :cursor AND D >= :start_date AND T IN (:doc_type_0)"
        self.assertEqual(self.errors({"start_date": "2025-01-01", "document_types": ["FT"]}, q), "")
        self.assertIn("data inválida", self.errors({"start_date": "31/01/2025"}, q))
        self.assertIn(":start_date", self.errors({"start_date": "2025-01-01"}))
        self.assertIn(":doc_type_0", self.errors({"document_types": ["FT"]}))
        self.assertIn("tipo inválido", self.errors({"document_types": ["FT'; DROP"]}, q))
        self.assertIn("só são aceites", self.errors({"outro": 1}))
        self.assertIn("até 50", self.errors({"document_types": ["FT"] * 51}))
