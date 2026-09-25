"""Assistente de configuração automática de ponta a ponta contra um SQL Server REAL (opcional).

Corre só com GATEWAY_IT_MSSQL_ADMIN=windows e GATEWAY_IT_MSSQL_DB (base de teste de
docs/teste_sqlserver.sql) num computador com SQL Server local. Cria o utilizador temporário
gateway_it_wizard e apaga-o no fim.
"""

import os
from unittest import mock, skipUnless

from sqlalchemy import create_engine, text

from invoices.models import Invoice
from sources.models import DataSource

from .tests import DashboardTestCase

DB = os.environ.get("GATEWAY_IT_MSSQL_DB")
TEMP_USER = "gateway_it_wizard"


@skipUnless(os.environ.get("GATEWAY_IT_MSSQL_ADMIN") == "windows" and DB, "SQL Server local de teste não configurado")
class WizardAgainstRealSqlServer(DashboardTestCase):
    def tearDown(self):
        from host_connector.provisioning import admin_config

        admin = admin_config("mssql", "localhost")
        engine = create_engine(admin.sqlalchemy_url().update_query_dict({"ApplicationIntent": "ReadWrite"}),
                               isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                conn.execute(text(f"USE [{DB}]; DROP USER IF EXISTS [{TEMP_USER}]"))
                conn.execute(text(f"IF SUSER_ID(N'{TEMP_USER}') IS NOT NULL DROP LOGIN [{TEMP_USER}]"))
                left = conn.execute(text(f"SELECT COUNT(*) FROM sys.server_principals WHERE name = N'{TEMP_USER}'"))
                assert left.scalar() == 0, "login temporário não apagado"
        finally:
            engine.dispose()
        super().tearDown()

    def test_full_wizard(self):
        self.login(self.admin)
        # 1-2. procura autorizada (real, neste computador)
        response = self.client.post("/configurar/", {"consent_local": "1"}, follow=True)
        servers = response.context["servers"]
        index = next(i for i, s in servers if s.engine == "mssql" and s.running)

        # 3. entrar com a conta Windows
        response = self.client.post("/configurar/ligar/", {"s": index, "auth": "windows", "consent_connect": "1"})
        self.assertEqual(response.status_code, 200)
        overview = response.context["overview"]
        names = [d.name for d in overview.databases]
        self.assertIn(DB, names)
        self.assertEqual(overview.databases[0].name, DB)  # a base de teste é a mais provável
        self.assertTrue(response.context["can_auto"])

        # 4-5. escolher a base e aprovar a criação do utilizador (nome temporário)
        with mock.patch("dashboard.setup.READONLY_USERNAME", TEMP_USER):
            confirm = self.client.post("/configurar/base/", {"access": response.context["access_token"],
                                                             "database": DB, "known": names, "mode": "auto"})
        self.assertContains(confirm, TEMP_USER)
        done = self.client.post("/configurar/criar-utilizador/", {
            "access": confirm.context["access_token"], "plan": confirm.context["plan_token"],
            "consent_provision": "1"})
        source = DataSource.objects.get(company=self.a, kind="DATABASE")
        self.assertRedirects(done, f"/ligacoes/{source.pk}/mapeamento/", fetch_redirect_response=False)
        self.assertEqual(source.db_user, TEMP_USER)

        # 6. mapeamento sugerido, pré-visualizado e guardado; sincronização
        page = self.client.get(f"/ligacoes/{source.pk}/mapeamento/")
        self.assertContains(page, "Sugestão automática")
        post = {k: v for k, v in page.context["form"].initial.items() if v not in (None, "")}
        post["action"] = "save"
        saved = self.client.post(f"/ligacoes/{source.pk}/mapeamento/", post, follow=True)
        self.assertContains(saved, "Mapeamento guardado")
        synced = self.client.post(f"/ligacoes/{source.pk}/sincronizar/", follow=True)
        self.assertContains(synced, "novos")
        self.assertGreater(Invoice.objects.filter(source=source).count(), 0)
        # O hash/assinatura da origem é lido e guardado tal como está no sistema de faturação.
        first = Invoice.objects.filter(source=source).order_by("source_document_id").first()
        self.assertTrue(first.document_hash)
        self.assertEqual(first.hash_control, "1")
