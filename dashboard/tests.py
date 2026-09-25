import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from agt.models import AgtConfiguration
from audit.models import AuditEvent
from companies.models import ApiKey, Company, Membership
from invoices.models import Invoice, InvoiceStatus
from invoices.services import import_document
from invoices.testing import document, numbered
from sources.models import DataSource

User = get_user_model()
Role = Membership.Role


class DashboardTestCase(TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="Hotel A", nif="5000000000")
        self.b = Company.objects.create(name="Hotel B", nif="5000000001")
        self.src_a = DataSource.objects.create(company=self.a, code="API", name="API", kind="API")
        self.src_b = DataSource.objects.create(company=self.b, code="API", name="API", kind="API")
        self.inv_a = import_document(self.src_a, numbered(1)).invoice
        self.inv_b = import_document(self.src_b, numbered(2)).invoice
        self.admin = self.user("admin", self.a, Role.ADMIN)
        self.operator = self.user("operador", self.a, Role.OPERATOR)
        self.viewer = self.user("consulta", self.a, Role.VIEWER)

    def user(self, name, company=None, role=None):
        user = User.objects.create_user(name, password="x")
        if company:
            Membership.objects.create(user=user, company=company, role=role)
        return user

    def login(self, user):
        self.client.force_login(user)


class AccessTests(DashboardTestCase):
    PAGES = ["/", "/faturas/", "/ligacoes/", "/agt/"]
    ADMIN_PAGES = ["/chaves-api/", "/auditoria/", "/ligacoes/nova/base-de-dados/", "/ligacoes/nova/api/"]

    def test_login_required(self):
        for name in self.PAGES + self.ADMIN_PAGES:
            response = self.client.get(name)
            self.assertEqual(response.status_code, 302, name)
            self.assertTrue(response["Location"].startswith("/entrar/"))

    def test_login_page_and_login(self):
        self.assertEqual(self.client.get("/entrar/").status_code, 200)
        response = self.client.post("/entrar/", {"username": "consulta", "password": "x"})
        self.assertRedirects(response, "/")
        self.client.logout()
        response = self.client.post("/entrar/", {"username": "consulta", "password": "errada"})
        self.assertContains(response, "incorretos")

    def test_logout_is_post_only(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get("/sair/").status_code, 405)
        self.client.post("/sair/")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_user_without_company(self):
        self.login(self.user("sem"))
        response = self.client.get("/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Sem acesso a empresas", status_code=403)

    def test_viewer_pages_and_admin_pages(self):
        self.login(self.viewer)
        for name in self.PAGES:
            self.assertEqual(self.client.get(name).status_code, 200, name)
        for name in self.ADMIN_PAGES:
            self.assertEqual(self.client.get(name).status_code, 403, name)
        self.login(self.admin)
        for name in self.PAGES + self.ADMIN_PAGES:
            self.assertEqual(self.client.get(name).status_code, 200, name)

    def test_menu_hides_admin_links_for_viewer(self):
        self.login(self.viewer)
        self.assertNotContains(self.client.get("/"), "Chaves de API")
        self.login(self.admin)
        self.assertContains(self.client.get("/"), "Chaves de API")


class IsolationTests(DashboardTestCase):
    def test_only_own_company_data(self):
        self.login(self.viewer)
        response = self.client.get("/faturas/")
        self.assertContains(response, "FT A2026/1")
        self.assertNotContains(response, "FT A2026/2")
        self.assertEqual(self.client.get(f"/faturas/{self.inv_b.pk}/").status_code, 404)

    def test_cannot_act_on_other_company_objects(self):
        self.login(self.admin)
        key_b, _ = ApiKey.generate(self.b, "B")
        self.assertEqual(self.client.post(f"/faturas/{self.inv_b.pk}/enviar/").status_code, 404)
        self.assertEqual(self.client.post(f"/chaves-api/{key_b.pk}/revogar/").status_code, 404)
        self.assertEqual(self.client.get(f"/ligacoes/{self.src_b.pk}/editar/").status_code, 404)
        self.assertEqual(self.client.post(f"/ligacoes/{self.src_b.pk}/sincronizar/").status_code, 404)
        key_b.refresh_from_db()
        self.assertTrue(key_b.active)

    def test_cannot_select_company_without_access(self):
        self.login(self.viewer)
        self.client.post("/empresa/", {"company": self.b.pk})
        self.assertNotContains(self.client.get("/faturas/"), "FT A2026/2")

    def test_select_company_with_access_and_safe_redirect(self):
        Membership.objects.create(user=self.viewer, company=self.b, role=Role.VIEWER)
        self.login(self.viewer)
        response = self.client.post("/empresa/", {"company": self.b.pk, "next": "/faturas/"})
        self.assertRedirects(response, "/faturas/")
        self.assertContains(self.client.get("/faturas/"), "FT A2026/2")
        for evil in ("https://mau.example/", "//mau.example/"):
            response = self.client.post("/empresa/", {"company": self.b.pk, "next": evil})
            self.assertEqual(response["Location"], "/")

    def test_superuser_sees_all_companies(self):
        root = User.objects.create_superuser("root", password="x")
        self.login(root)
        self.assertContains(self.client.get("/"), "Hotel B")

    def test_inactive_company_hidden(self):
        Company.objects.filter(pk=self.a.pk).update(active=False)
        self.login(self.admin)
        self.assertEqual(self.client.get("/").status_code, 403)


class InvoiceViewTests(DashboardTestCase):
    def test_list_filters(self):
        import_document(self.src_a, numbered(3, total="1.00", customer_name="Cliente Especial"))
        self.login(self.viewer)
        self.assertContains(self.client.get("/faturas/?grupo=erros"), "FT A2026/3")
        self.assertNotContains(self.client.get("/faturas/?grupo=erros"), "FT A2026/1<")
        self.assertContains(self.client.get("/faturas/?q=Especial"), "FT A2026/3")
        self.assertContains(self.client.get("/faturas/?estado=VALIDATED"), "FT A2026/1")
        self.assertNotContains(self.client.get("/faturas/?de=2026-02-01"), "FT A2026/1")
        self.assertContains(self.client.get("/faturas/?de=nao-e-data"), "Data inválida")
        self.assertContains(self.client.get("/faturas/?origem=API"), "FT A2026/1")

    def test_pagination_keeps_filters(self):
        for n in range(10, 70):
            import_document(self.src_a, numbered(n))
        self.login(self.viewer)
        response = self.client.get("/faturas/?grupo=pendentes")
        self.assertContains(response, "grupo=pendentes&amp;page=2")

    def test_detail_and_xss_escaped(self):
        invoice = import_document(self.src_a, numbered(4, customer_name="<script>alert(1)</script>")).invoice
        self.login(self.viewer)
        response = self.client.get(f"/faturas/{invoice.pk}/")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "Enviar à AGT")  # consulta não tem ações

    def test_viewer_cannot_act(self):
        self.login(self.viewer)
        self.assertEqual(self.client.post(f"/faturas/{self.inv_a.pk}/enviar/").status_code, 403)
        self.inv_a.refresh_from_db()
        self.assertEqual(self.inv_a.status, InvoiceStatus.VALIDATED)

    def test_actions_are_post_only(self):
        self.login(self.operator)
        self.assertEqual(self.client.get(f"/faturas/{self.inv_a.pk}/enviar/").status_code, 405)

    def test_send_process_and_audit(self):
        self.login(self.operator)
        detail = self.client.get(f"/faturas/{self.inv_a.pk}/")
        self.assertContains(detail, "Enviar à AGT")
        self.client.post(f"/faturas/{self.inv_a.pk}/enviar/")
        self.inv_a.refresh_from_db()
        self.assertEqual(self.inv_a.status, InvoiceStatus.QUEUED)
        self.assertTrue(AuditEvent.objects.filter(action="INVOICE_ENVIAR", user=self.operator).exists())

        response = self.client.post("/fila/processar/", follow=True)
        self.assertContains(response, "1 validados")
        self.inv_a.refresh_from_db()
        self.assertEqual(self.inv_a.status, InvoiceStatus.CONFIRMED)
        self.assertTrue(self.inv_a.simulated)
        self.assertEqual(Invoice.objects.get(pk=self.inv_b.pk).status, InvoiceStatus.VALIDATED)  # outra empresa

    def test_invalid_action_messages(self):
        self.login(self.operator)
        response = self.client.post(f"/faturas/{self.inv_a.pk}/reenviar/", follow=True)
        self.assertContains(response, "Não é possível enviar")
        bad = import_document(self.src_a, numbered(5, total="1.00")).invoice
        response = self.client.post(f"/faturas/{bad.pk}/enviar/", follow=True)
        self.assertContains(response, "erros de validação")
        response = self.client.post(f"/faturas/{self.inv_a.pk}/xpto/")
        self.assertEqual(response.status_code, 302)

    def test_revalidate(self):
        self.login(self.operator)
        response = self.client.post(f"/faturas/{self.inv_a.pk}/revalidar/", follow=True)
        self.assertContains(response, "Documento válido.")

    def test_home_banners(self):
        self.login(self.viewer)
        self.assertContains(self.client.get("/"), "simulação")
        AgtConfiguration.objects.filter(company=self.a).update(environment="PRODUCTION")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertContains(self.client.get("/"), "Configuração AGT incompleta")


class AgtViewTests(DashboardTestCase):
    def form_data(self, **changes):
        data = {"environment": "SIMULATION", "base_url": "", "software_certificate_number": "", "software_name": "",
                "software_version": "", "client_id": "", "credentials_prefix": "", "timeout_seconds": 30,
                "max_attempts": 5, "retry_delay_seconds": 300}
        data.update(changes)
        return data

    def test_admin_edits(self):
        self.login(self.admin)
        response = self.client.post("/agt/", self.form_data(auto_send="on", software_name="Gateway"))
        self.assertRedirects(response, "/agt/")
        config = AgtConfiguration.objects.get(company=self.a)
        self.assertTrue(config.auto_send)
        self.assertEqual(config.updated_by, self.admin)
        self.assertTrue(AuditEvent.objects.filter(action="AGT_CONFIG_UPDATED").exists())

    def test_production_requires_fields(self):
        self.login(self.admin)
        response = self.client.post("/agt/", self.form_data(environment="PRODUCTION"))
        self.assertContains(response, "Obrigatório fora do modo simulação")
        response = self.client.post("/agt/", self.form_data(base_url="http://inseguro.example/"))
        self.assertContains(response, "https://")

    def test_non_admin_read_only(self):
        self.login(self.operator)
        response = self.client.get("/agt/")
        self.assertContains(response, "Só administradores")
        self.client.post("/agt/", self.form_data(auto_send="on"))
        self.assertFalse(AgtConfiguration.objects.get(company=self.a).auto_send)

    def test_secrets_status_never_values(self):
        self.login(self.admin)
        with mock.patch.dict(os.environ, {"AGT_CLIENT_SECRET": "valor-muito-secreto"}):
            response = self.client.get("/agt/")
        self.assertContains(response, "AGT_CLIENT_SECRET")
        self.assertContains(response, "definida")
        self.assertNotContains(response, "valor-muito-secreto")


class ApiKeyViewTests(DashboardTestCase):
    def test_create_shows_once_and_works(self):
        self.login(self.admin)
        response = self.client.post("/chaves-api/", {"name": "ERP", "source": self.src_a.pk})
        raw = response.context["new_key"]
        self.assertContains(response, raw)
        self.assertEqual(ApiKey.authenticate(raw).source, self.src_a)
        self.assertNotContains(self.client.get("/chaves-api/"), raw)
        api = self.client.get("/api/v1/ping/", HTTP_AUTHORIZATION=f"Api-Key {raw}")
        self.assertEqual(api.status_code, 200)

    def test_cannot_use_other_company_or_database_source(self):
        db = DataSource.objects.create(company=self.a, code="HOST", name="H", kind="DATABASE", connection_mode="ENV", connection="default")
        self.login(self.admin)
        for source in (self.src_b, db):
            response = self.client.post("/chaves-api/", {"name": "x", "source": source.pk})
            self.assertIsNone(response.context["new_key"])
        self.assertEqual(ApiKey.objects.count(), 0)

    def test_revoke(self):
        key, raw = ApiKey.generate(self.a, "ERP", self.src_a)
        self.login(self.admin)
        self.client.post(f"/chaves-api/{key.pk}/revogar/")
        self.assertIsNone(ApiKey.authenticate(raw))
        self.assertTrue(AuditEvent.objects.filter(action="API_KEY_REVOKED").exists())

    def test_no_api_source_hint(self):
        DataSource.objects.filter(pk=self.src_a.pk).update(active=False)
        self.login(self.admin)
        self.assertContains(self.client.get("/chaves-api/"), "Crie primeiro")


class AuditViewTests(DashboardTestCase):
    def test_tabs_and_isolation(self):
        self.login(self.admin)
        self.client.post("/agt/", {"environment": "SIMULATION", "timeout_seconds": 30, "max_attempts": 5,
                                   "retry_delay_seconds": 300})
        response = self.client.get("/auditoria/")
        self.assertContains(response, "AGT_CONFIG_UPDATED")
        response = self.client.get("/auditoria/?tab=integracao")
        self.assertContains(response, "IMPORT")
        self.assertContains(response, "FT A2026/1")
        self.assertNotContains(response, "FT A2026/2")
