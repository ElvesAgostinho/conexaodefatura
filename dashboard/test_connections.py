"""Páginas de ligações (base de dados e API), assistente de mapeamento e página inicial."""

import json

from audit.models import AuditEvent
from companies.models import ApiKey
from invoices.models import Invoice
from sources.models import DataSource
from sources.test_connections import make_fake_db

from .tests import DashboardTestCase


class ConnectionFormTests(DashboardTestCase):
    def db_data(self, **changes):
        data = {"code": "ERP", "name": "ERP demo", "system": "ERP", "active": "on", "connection_mode": "PANEL",
                "db_engine": "postgresql", "db_host": "10.0.0.5", "db_port": "", "db_name": "erp",
                "db_user": "leitura", "db_password": "Senha-Secreta-99", "db_odbc_driver": "", "db_options": "",
                "connection": "", "action": "save"}
        data.update(changes)
        return data

    def test_list_shows_connect_buttons_only_for_admin(self):
        self.login(self.admin)
        page = self.client.get("/ligacoes/")
        self.assertContains(page, "Ligar base de dados")
        self.assertContains(page, "Ligar por API")
        self.login(self.viewer)
        self.assertNotContains(self.client.get("/ligacoes/"), "Ligar base de dados")

    def test_create_database_connection_password_encrypted_never_shown(self):
        self.login(self.admin)
        response = self.client.post("/ligacoes/nova/base-de-dados/", self.db_data())
        source = DataSource.objects.get(code="ERP")
        self.assertRedirects(response, f"/ligacoes/{source.pk}/")
        self.assertEqual((source.kind, source.connection_mode, source.db_host), ("DATABASE", "PANEL", "10.0.0.5"))
        self.assertNotIn("Senha-Secreta-99", source.db_password_encrypted)
        self.assertEqual(source.connection_config().password, "Senha-Secreta-99")
        for url in (f"/ligacoes/{source.pk}/", f"/ligacoes/{source.pk}/editar/", "/ligacoes/", "/auditoria/"):
            self.assertNotContains(self.client.get(url), "Senha-Secreta-99", msg_prefix=url)
        self.assertContains(self.client.get(f"/ligacoes/{source.pk}/"), "guardada cifrada")
        event = AuditEvent.objects.get(action="SOURCE_CREATED")
        self.assertNotIn("Senha-Secreta-99", json.dumps(event.details))
        self.assertIn("senha", event.details["changed"])

    def test_edit_keeps_or_clears_password(self):
        self.login(self.admin)
        self.client.post("/ligacoes/nova/base-de-dados/", self.db_data())
        source = DataSource.objects.get(code="ERP")
        self.client.post(f"/ligacoes/{source.pk}/editar/", self.db_data(db_password="", db_host="10.0.0.6"))
        source.refresh_from_db()
        self.assertEqual((source.db_host, source.connection_config().password), ("10.0.0.6", "Senha-Secreta-99"))
        self.client.post(f"/ligacoes/{source.pk}/editar/", self.db_data(db_password="", clear_password="on"))
        source.refresh_from_db()
        self.assertFalse(source.has_db_password)

    def test_validation_errors(self):
        self.login(self.admin)
        self.assertContains(self.client.post("/ligacoes/nova/base-de-dados/", self.db_data(db_host="")), "Obrigatório")
        self.assertContains(self.client.post("/ligacoes/nova/base-de-dados/", self.db_data(code="API")),
                            "Já existe uma ligação")
        self.assertContains(self.client.post("/ligacoes/nova/base-de-dados/",
                                             self.db_data(connection_mode="ENV", connection="")), "nome da ligação")
        self.assertFalse(DataSource.objects.filter(code="ERP").exists())

    def test_test_without_saving(self):
        path = make_fake_db(self)
        self.login(self.admin)
        response = self.client.post("/ligacoes/nova/base-de-dados/",
                                    self.db_data(db_engine="sqlite", db_name=path, db_host="", db_user="",
                                                 db_password="", action="test"))
        self.assertContains(response, "Ligação OK")
        self.assertFalse(DataSource.objects.filter(code="ERP").exists())

        response = self.client.post("/ligacoes/nova/base-de-dados/",
                                    self.db_data(db_engine="sqlite", db_name=path + ".nao", db_host="", action="test"))
        self.assertContains(response, "não encontrado")
        # A senha escrita volta ao formulário só depois de testar, para não ter de a reescrever.
        self.assertContains(response, "Senha-Secreta-99")
        response = self.client.post("/ligacoes/nova/base-de-dados/", self.db_data(db_host="", action="test"))
        self.assertContains(response, "Corrija os campos")

    def test_api_connection_and_unknown_kind(self):
        self.login(self.admin)
        response = self.client.post("/ligacoes/nova/api/", {"code": "POS", "name": "POS", "system": "", "active": "on"})
        source = DataSource.objects.get(code="POS")
        self.assertRedirects(response, f"/ligacoes/{source.pk}/")
        self.assertEqual(source.kind, "API")
        self.assertEqual(self.client.get("/ligacoes/nova/ftp/").status_code, 404)

    def test_permissions(self):
        self.login(self.operator)
        self.assertEqual(self.client.get("/ligacoes/nova/base-de-dados/").status_code, 403)
        self.assertEqual(self.client.post("/ligacoes/nova/api/", {"code": "X", "name": "X"}).status_code, 403)


class ApiConnectionDetailTests(DashboardTestCase):
    def test_endpoint_examples_and_key_shown_once(self):
        self.login(self.admin)
        page = self.client.get(f"/ligacoes/{self.src_a.pk}/")
        self.assertContains(page, "/api/v1/documents/")
        self.assertContains(page, "Invoke-RestMethod")
        self.assertContains(page, "&lt;código de isenção&gt;")
        response = self.client.post(f"/ligacoes/{self.src_a.pk}/chaves/", {"name": "ERP", "source": self.src_a.pk},
                                    follow=True)
        key = ApiKey.objects.get(name="ERP")
        raw = response.context["new_key"]
        self.assertTrue(raw.startswith(f"gw_{key.prefix}_"))
        self.assertContains(response, raw)
        self.assertNotContains(self.client.get(f"/ligacoes/{self.src_a.pk}/"), raw)  # só uma vez
        self.assertEqual(self.client.get("/api/v1/ping/", HTTP_AUTHORIZATION=f"Api-Key {raw}").status_code, 200)

    def test_key_cannot_be_created_for_other_source(self):
        other = DataSource.objects.create(company=self.a, code="POS", name="POS", kind="API")
        self.login(self.admin)
        self.client.post(f"/ligacoes/{self.src_a.pk}/chaves/", {"name": "x", "source": other.pk})
        self.assertFalse(ApiKey.objects.exists())
        self.assertEqual(self.client.post(f"/ligacoes/{self.src_b.pk}/chaves/", {"name": "x"}).status_code, 404)

    def test_viewer_cannot_create_key(self):
        self.login(self.viewer)
        self.assertEqual(self.client.post(f"/ligacoes/{self.src_a.pk}/chaves/", {"name": "x"}).status_code, 403)
        self.assertNotContains(self.client.get(f"/ligacoes/{self.src_a.pk}/"), "Criar chave")


class DatabaseConnectionPagesTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.path = make_fake_db(self)
        self.db_source = DataSource.objects.create(company=self.a, code="ERP", name="ERP", kind="DATABASE",
                                                   db_engine="sqlite", db_name=self.path)

    def url(self, suffix=""):
        return f"/ligacoes/{self.db_source.pk}/{suffix}"

    def assistant_post(self, action="preview", **changes):
        data = {"cursor_column": "Id", "cursor_type": "int", "link_column": "DocId", "batch_size": "200",
                "h_source_document_id": "Id", "h_document_type": "Tipo", "h_series": "Serie",
                "h_document_number": "Numero", "h_document_date": "Data", "h_customer_name": "Cliente",
                "h_subtotal": "Base", "h_tax_amount": "Iva", "h_total": "Total", "hd_currency": "AOA",
                "l_line_number": "Ord", "l_description": "Descr", "l_quantity": "Qtd", "l_unit_price": "Preco",
                "l_tax_rate": "Taxa", "l_tax_amount": "Imposto", "l_total": "Valor", "document_types": ["FT"], "action": action}
        data.update(changes)
        return self.client.post(self.url("mapeamento/") + "?documents_table=Doc+Cab&lines_table=DocLin", data,
                                follow=True)

    def test_detail_steps_and_test_button(self):
        self.login(self.operator)
        page = self.client.get(self.url())
        self.assertContains(page, "Configuração passo a passo")
        self.assertContains(page, "SQLite")
        response = self.client.post(self.url("testar/"), follow=True)
        self.assertContains(response, "Ligação OK")

    def test_test_redirect_is_safe(self):
        self.login(self.operator)
        for evil in ("https://mau.example/", "//mau.example/", "/\\mau.example"):
            response = self.client.post(self.url("testar/"), {"next": evil})
            self.assertEqual(response["Location"], self.url(), evil)
        response = self.client.post(self.url("testar/"), {"next": "/ligacoes/"})
        self.assertEqual(response["Location"], "/ligacoes/")

    def test_explore(self):
        self.login(self.operator)
        page = self.client.get(self.url("estrutura/"))
        self.assertContains(page, "Doc Cab")
        self.assertContains(page, "vwDocs")
        page = self.client.get(self.url("estrutura/") + "?tabela=Doc+Cab")
        self.assertContains(page, "Numero")
        self.assertContains(page, "FT A/2")  # amostra
        response = self.client.get(self.url("estrutura/") + "?tabela=Nao+Existe", follow=True)
        self.assertContains(response, "não existe")
        self.assertEqual(self.client.get(f"/ligacoes/{self.src_a.pk}/estrutura/").status_code, 404)  # API
        self.login(self.viewer)
        self.assertEqual(self.client.get(self.url("estrutura/")).status_code, 403)

    def test_explore_with_broken_connection(self):
        DataSource.objects.filter(pk=self.db_source.pk).update(db_name=self.path + ".nao")
        self.login(self.operator)
        response = self.client.get(self.url("estrutura/"), follow=True)
        self.assertContains(response, "Ligação por configurar")

    def test_assistant_full_flow(self):
        self.login(self.admin)
        page = self.client.get(self.url("mapeamento/"))
        self.assertContains(page, "1. Tabelas")
        # Sem mapeamento, o assistente já abre com a sugestão feita a partir da estrutura real.
        self.assertContains(page, "Sugestão automática")
        self.assertEqual(page.context["tables_form"].initial["documents_table"], "Doc Cab")
        self.assertEqual(page.context["form"].initial["h_total"], "Total")
        page = self.client.get(self.url("mapeamento/") + "?documents_table=Doc+Cab&lines_table=DocLin")
        self.assertContains(page, "3. Cabeçalho do documento")

        preview = self.assistant_post("preview")
        self.assertContains(preview, "Pré-visualização")
        self.assertContains(preview, "válido")
        self.assertContains(preview, "FT A/1")
        self.db_source.refresh_from_db()
        self.assertEqual(self.db_source.mapping, {})  # pré-visualizar não guarda
        self.assertEqual(Invoice.objects.filter(source=self.db_source).count(), 0)

        saved = self.assistant_post("save")
        self.assertContains(saved, "Mapeamento guardado")
        self.db_source.refresh_from_db()
        self.assertEqual(self.db_source.mapping["assistant"]["documents_table"], "Doc Cab")

        # Voltar ao assistente pré-preenche as escolhas.
        page = self.client.get(self.url("mapeamento/"))
        self.assertEqual(page.context["form"].initial["h_total"], "Total")

        response = self.client.post(self.url("sincronizar/"), follow=True)
        self.assertContains(response, "2 novos")
        self.assertEqual(Invoice.objects.filter(source=self.db_source).count(), 2)

    def test_assistant_required_fields_and_bad_choices(self):
        self.login(self.admin)
        response = self.assistant_post("save", h_total="", h_source_document_id="")
        self.assertContains(response, "Escolha uma coluna ou indique um valor fixo")
        self.assertContains(response, "tem de vir de uma coluna")
        response = self.assistant_post("save", h_total="Coluna; DROP TABLE x")
        self.assertIn("h_total", response.context["form"].errors)  # só colunas reais são aceites
        response = self.client.get(self.url("mapeamento/") + "?documents_table=Falsa&lines_table=DocLin", follow=True)
        self.assertContains(response, "não existe")
        self.db_source.refresh_from_db()
        self.assertEqual(self.db_source.mapping, {})

    def test_assistant_permissions(self):
        self.login(self.operator)
        self.assertEqual(self.client.get(self.url("mapeamento/")).status_code, 403)
        self.assertEqual(self.client.get(self.url("mapeamento/json/")).status_code, 403)

    def test_mapping_json_editor(self):
        self.login(self.admin)
        response = self.client.post(self.url("mapeamento/json/"),
                                    {"mapping": json.dumps({"documents_query": "DELETE FROM x"})})
        self.assertContains(response, "documents_query")
        self.db_source.refresh_from_db()
        self.assertEqual(self.db_source.mapping, {})
        self.assertContains(self.client.post(self.url("mapeamento/json/"), {"mapping": "{nao json"}), "JSON")


class HomeChartTests(DashboardTestCase):
    def test_home_chart_and_distribution(self):
        self.login(self.viewer)
        page = self.client.get("/")
        self.assertContains(page, "Documentos por dia")
        self.assertContains(page, 'class="chart"')
        self.assertContains(page, "Ver como tabela")
        self.assertContains(page, "Estado dos documentos")
        distribution = page.context["distribution"]
        self.assertEqual(distribution["total"], 1)
        self.assertEqual([s["key"] for s in distribution["segments"]], ["enviadas", "pendentes", "rejeitadas", "erros"])
        self.assertEqual(sum(s["pct"] for s in distribution["segments"]), 100.0)

    def test_chart_geometry(self):
        from datetime import timedelta

        from django.utils import timezone

        from dashboard.charts import _nice_max, daily_documents
        from invoices.testing import numbered
        from invoices.services import import_document

        today = timezone.localdate()
        for n in range(3, 8):
            import_document(self.src_a, numbered(n, document_date=str(today - timedelta(days=1))))
        chart = daily_documents(Invoice.objects.filter(company=self.a))
        self.assertEqual(len(chart["bars"]), 14)
        yesterday = chart["bars"][-2]
        self.assertEqual(yesterday["count"], 5)
        self.assertTrue(yesterday["d"].startswith("M"))
        self.assertEqual(chart["bars"][-1]["d"], "")  # hoje: sem documentos, sem barra
        self.assertTrue(all(isinstance(t["value"], int) for t in chart["ticks"]))
        self.assertEqual([_nice_max(v) for v in (0, 3, 5, 11, 26, 101)], [4, 4, 5, 20, 50, 200])
        self.assertEqual(daily_documents(Invoice.objects.none())["total"], 0)

    def test_svg_coordinates_use_decimal_point(self):
        # Regressão: com LANGUAGE_CODE=pt-pt o Django escrevia "12,5" nas coordenadas e o gráfico partia.
        import re

        self.login(self.viewer)
        html = self.client.get("/").content.decode()
        svg = html[html.index('<svg class="chart"'):html.index("</svg>", html.index('<svg class="chart"'))]
        self.assertFalse(re.search(r'(x|y|width|height|x1|x2|y1|y2)="-?\d+,\d', svg), "vírgula decimal no SVG")
        self.assertFalse(re.search(r"translate\(\d+,\d+,", svg))
        self.assertRegex(svg, r'x="\d+\.\d+"')

    def test_nav_counts_and_env_pill(self):
        self.login(self.viewer)
        page = self.client.get("/")
        self.assertEqual(page.context["nav_counts"]["pendentes"], 1)
        self.assertContains(page, "env-pill sim")


class AssistantFilterPageTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        from sources.test_filters import make_db

        self.path, _ = make_db(self)
        self.src = DataSource.objects.create(company=self.a, code="ERP", name="ERP", kind="DATABASE",
                                             db_engine="sqlite", db_name=self.path)
        self.login(self.admin)
        self.url = f"/ligacoes/{self.src.pk}/mapeamento/?documents_table=Docs&lines_table=Linhas"

    def post(self, action="save", **extra):
        data = {"cursor_column": "Id", "cursor_type": "int", "link_column": "DocId", "batch_size": "200",
                "h_source_document_id": "Id", "h_document_type": "Tipo", "h_series": "Serie",
                "h_document_number": "Numero", "h_document_date": "Data", "h_customer_name": "Cliente",
                "h_subtotal": "Base", "h_tax_amount": "Iva", "h_total": "Total", "hd_currency": "AOA",
                "l_line_number": "Ord", "l_description": "Descr", "l_quantity": "Qtd", "l_unit_price": "Preco",
                "l_tax_rate": "Taxa", "l_tax_amount": "Imposto", "l_total": "Valor", "action": action}
        data.update(extra)
        return self.client.post(self.url, data, follow=True)

    def test_types_listed_with_counts_and_all_checked(self):
        page = self.client.get(self.url)
        self.assertContains(page, "5. O que importar")
        self.assertContains(page, "FT (3)")
        self.assertContains(page, "PP (1)")
        self.assertEqual(sorted(page.context["form"].initial["document_types"]), ["FR", "FT", "NC", "OR", "PP"])

    def test_all_checked_saves_no_filter(self):
        self.post(document_types=["FT", "PP", "FR", "OR", "NC"])
        self.src.refresh_from_db()
        self.assertEqual(self.src.mapping["filters"], {})

    def test_unchecking_types_and_start_date_saved_and_prefilled(self):
        response = self.post(document_types=["FT", "FR", "NC"], start_date="2025-01-01")
        self.assertContains(response, "Mapeamento guardado")
        self.src.refresh_from_db()
        self.assertEqual(self.src.mapping["filters"], {"start_date": "2025-01-01", "document_types": ["FT", "FR", "NC"]})
        page = self.client.get(f"/ligacoes/{self.src.pk}/mapeamento/")
        self.assertEqual(sorted(page.context["form"].initial["document_types"]), ["FR", "FT", "NC"])
        self.assertEqual(page.context["form"].initial["start_date"], "2025-01-01")
        sync = self.client.post(f"/ligacoes/{self.src.pk}/sincronizar/", follow=True)
        self.assertContains(sync, "4 novos")

    def test_validation(self):
        response = self.post(document_types=[])
        self.assertContains(response, "Escolha pelo menos um tipo")
        response = self.post(document_types=["FT"], start_after="abc")
        self.assertContains(response, "indique um número")
        response = self.post(document_types=["XX"])
        self.assertIn("document_types", response.context["form"].errors)
        self.src.refresh_from_db()
        self.assertEqual(self.src.mapping, {})

    def test_start_after_id(self):
        self.post(document_types=["FT", "PP", "FR", "OR", "NC"], start_after="5")
        self.src.refresh_from_db()
        self.assertEqual(self.src.mapping["initial_cursor"], "5")
        sync = self.client.post(f"/ligacoes/{self.src.pk}/sincronizar/", follow=True)
        self.assertContains(sync, "2 novos")
