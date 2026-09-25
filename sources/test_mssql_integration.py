"""Teste de integração contra um SQL Server REAL (opcional).

Só corre se estas variáveis estiverem definidas (senão é saltado):
    GATEWAY_IT_MSSQL_HOST       ex.: localhost ou SERVIDOR\\INSTANCIA
    GATEWAY_IT_MSSQL_DB         base de teste com dbo.Documentos e dbo.DocumentoLinhas
                                (script de exemplo em docs/teste_sqlserver.sql)
    GATEWAY_IT_MSSQL_USER       utilizador SÓ DE LEITURA (db_datareader)
    GATEWAY_IT_MSSQL_PASSWORD
    GATEWAY_IT_MSSQL_ADMIN=windows  (opcional) testa também a criação automática do utilizador
                                    só de leitura com a conta Windows de administrador; o
                                    utilizador de teste é apagado no fim.
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


@skipUnless(all(ENV.values()), "SQL Server de teste não configurado (GATEWAY_IT_MSSQL_*)")
class RealSqlServerAutoSetupTests(TestCase):
    def test_discovery_finds_the_server(self):
        from host_connector.discovery import discover_local

        if ENV["HOST"].lower() not in ("localhost", "127.0.0.1", "."):
            self.skipTest("Servidor de teste não é local.")
        self.assertIn("mssql", [s.engine for s in discover_local().servers])

    def test_autodetect_suggests_complete_mapping(self):
        from sources.autodetect import suggest

        source = DataSource(company=Company(name="x", nif="1"), code="X", name="X", kind="DATABASE",
                            db_engine="mssql", db_host=ENV["HOST"], db_name=ENV["DB"], db_user=ENV["USER"],
                            db_trust_server_certificate=True)
        source.set_db_password(ENV["PASSWORD"])
        db = HostDatabase(source.connection_config())
        self.addCleanup(db.dispose)
        s = suggest(db)
        self.assertTrue(s.complete, s.notes)
        self.assertEqual((s.documents_table.qualified, s.link_column), ("dbo.Documentos", "DocumentoId"))
        self.assertEqual(len(s.header), 13)  # 11 campos + hash e controlo do hash
        self.assertEqual((s.header['document_hash'], s.header['hash_control']), ('Hash', 'HashControl'))
        self.assertEqual(len(s.lines), 10)

    @skipUnless(os.environ.get("GATEWAY_IT_MSSQL_ADMIN") == "windows", "Sem conta Windows de administrador")
    def test_provisioning_creates_read_only_user_and_cleans_up(self):
        from host_connector.config import HostConnectionConfig
        from host_connector.provisioning import (admin_config, apply_plan, provisioning_plan, server_overview,
                                                 verify_read_only)

        admin = admin_config("mssql", ENV["HOST"])
        overview = server_overview(admin)
        self.assertTrue(overview.can_create_logins)
        names = [d.name for d in overview.databases]
        self.assertIn(ENV["DB"], names)
        self.assertTrue(next(d for d in overview.databases if d.name == ENV["DB"]).likely)
        plan = provisioning_plan(admin, ENV["DB"], username="gateway_it_auto", known_databases=names)
        self.addCleanup(self._drop_login, admin, "gateway_it_auto")
        apply_plan(admin, plan)
        apply_plan(admin, plan)  # repetir é seguro
        user = HostConnectionConfig.from_values(engine="mssql", host=ENV["HOST"], name=ENV["DB"],
                                                user=plan.username, password=plan.password,
                                                trust_server_certificate=True)
        self.assertTrue(verify_read_only(user).ok)

    @staticmethod
    def _drop_login(admin, name):
        from sqlalchemy import create_engine

        engine = create_engine(admin.sqlalchemy_url().update_query_dict({"ApplicationIntent": "ReadWrite"}),
                               isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                conn.execute(text(f"USE [{ENV['DB']}]; DROP USER IF EXISTS [{name}]"))
                conn.execute(text(f"IF SUSER_ID(N'{name}') IS NOT NULL DROP LOGIN [{name}]"))
                left = conn.execute(text(f"SELECT COUNT(*) FROM sys.server_principals WHERE name = N'{name}'")).scalar()
                assert left == 0, "o login de teste não foi apagado"
        finally:
            engine.dispose()
