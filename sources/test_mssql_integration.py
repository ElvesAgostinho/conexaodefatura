"""Teste de integração contra um SQL Server REAL (opcional).

Só corre se estas variáveis estiverem definidas (senão é saltado):
    GATEWAY_IT_MSSQL_HOST       ex.: localhost ou SERVIDOR\\INSTANCIA
    GATEWAY_IT_MSSQL_DB         base de teste com dbo.Documentos e dbo.DocumentoLinhas
                                (script de exemplo em docs/teste_sqlserver.sql)
    GATEWAY_IT_MSSQL_USER       utilizador SÓ DE LEITURA (db_datareader)
    GATEWAY_IT_MSSQL_PASSWORD
"""

import os
from unittest import skipUnless

from django.test import TestCase
from sqlalchemy import text

from companies.models import Company
from host_connector.catalog import list_tables, resolve_table, table_columns
from host_connector.connection import HostDatabase
from host_connector.permissions import check_permissions
from invoices.models import Invoice, InvoiceStatus
from sources.assistant import AssistantChoices, build_mapping, preview
from sources.models import DataSource
from sources.sync import sync_source

ENV = {k: os.environ.get(f"GATEWAY_IT_MSSQL_{k}") for k in ("HOST", "DB", "USER", "PASSWORD")}


@skipUnless(all(ENV.values()), "SQL Server de teste não configurado (GATEWAY_IT_MSSQL_*)")
class RealSqlServerTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Teste", nif="5000000000")
        self.source = DataSource(company=self.company, code="SQLIT", name="SQL Server IT", kind="DATABASE",
                                 db_engine="mssql", db_host=ENV["HOST"], db_name=ENV["DB"], db_user=ENV["USER"],
                                 db_trust_server_certificate=True)
        self.source.set_db_password(ENV["PASSWORD"])
        self.source.full_clean()
        self.source.save()
        self.db = HostDatabase(self.source.connection_config())
        self.addCleanup(self.db.dispose)

    def mapping(self):
        tables = list_tables(self.db)
        docs = resolve_table(self.db, "dbo.Documentos", tables)
        lines = resolve_table(self.db, "dbo.DocumentoLinhas", tables)
        return build_mapping(self.db, AssistantChoices(
            documents_table=docs, lines_table=lines,
            header_columns={"source_document_id": "Id", "document_type": "TipoDoc", "series": "Serie",
                            "document_number": "NumDoc", "document_date": "DataDoc", "customer_name": "ClienteNome",
                            "customer_nif": "ClienteNIF", "currency": "Moeda", "subtotal": "TotalLiquido",
                            "tax_amount": "TotalIVA", "total": "TotalDocumento"},
            header_defaults={},
            line_columns={"line_number": "NumLinha", "product_code": "CodArtigo", "description": "Descricao",
                          "quantity": "Quantidade", "unit_price": "PrecoUnitario", "discount": "Desconto",
                          "tax_rate": "TaxaIVA", "tax_amount": "ValorIVA", "total": "TotalLinha",
                          "tax_exemption_code": "CodIsencao"},
            line_defaults={}, cursor_column="Id", cursor_type="int", link_column="DocumentoId",
        ), {c["name"] for c in table_columns(self.db, docs)}, {c["name"] for c in table_columns(self.db, lines)})

    def test_connection_is_read_only(self):
        result = self.db.test_connection()
        self.assertTrue(result.ok, result.message)
        self.assertIn("ApplicationIntent=ReadOnly", " ".join(result.read_only_measures))
        report = check_permissions(self.db)
        self.assertTrue(report.read_only, report.write_permissions)
        with self.assertRaises(Exception):
            with self.db.connect() as conn:  # contorna a guarda de SQL: é o próprio SQL Server que recusa
                conn.execute(text("CREATE TABLE gateway_it_escrita (x int)"))

    def test_generated_sql_uses_brackets_and_columns_with_spaces_are_readable(self):
        mapping = self.mapping()
        self.assertIn("[Id]", mapping["documents_query"])
        from host_connector.catalog import sample_rows

        names, rows = sample_rows(self.db, resolve_table(self.db, "dbo.Documentos"))
        self.assertIn("Obs Interna", names)
        self.assertTrue(rows)

    def test_full_flow(self):
        mapping = self.mapping()
        items = preview(self.db, mapping, limit=10)
        self.assertTrue(any(i.ok for i in items))
        self.assertTrue(any("Total" in " ".join(i.errors) for i in items))  # documento com total errado
        self.assertEqual(Invoice.objects.count(), 0)  # pré-visualizar não importa

        self.source.mapping = mapping
        self.source.save()
        first = sync_source(self.source)
        self.assertGreater(first.created, 0)
        self.assertEqual(first.stop_reason, "")
        second = sync_source(self.source)
        self.assertEqual(second.created, 0)  # idempotente
        self.assertTrue(Invoice.objects.filter(status=InvoiceStatus.ERROR).exists())
        self.assertTrue(Invoice.objects.filter(customer_name__contains="ç").exists())  # acentos preservados
