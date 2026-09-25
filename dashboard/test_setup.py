"""Configuração automática: autorizações, segredos, tokens e fluxo completo (servidor simulado)."""

import json
import time
from unittest import mock

from audit.models import AuditEvent
from config.crypto import DecryptionError, decrypt_temporary, encrypt_temporary
from host_connector.discovery import DetectedServer, DiscoveryResult
from host_connector.provisioning import DatabaseInfo, ProvisioningError, ServerOverview, Verification
from sources.models import DataSource
from sources.test_autodetect import WITH_FK, fake_db

from .tests import DashboardTestCase

ADMIN_PW = "SenhaDoAdmin#123"


def overview(**kw):
    data = dict(version="16.0", login="DOMINIO\\admin", can_create_logins=True, sql_logins_allowed=True,
                databases=[DatabaseInfo("HOTEL", score=12, tables=40, hints=["Faturas"]), DatabaseInfo("Outra")])
    data.update(kw)
    return ServerOverview(**data)


def local_result():
    return DiscoveryResult(servers=[
        DetectedServer("mssql", "localhost", port=1433, instance="MSSQLSERVER", running=True, version="16.0",
                       evidence=["serviço do Windows"]),
        DetectedServer("mysql", "localhost", port=3306, running=False, evidence=["serviço do Windows"]),
    ])


class SetupTestCase(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def start(self, **data):
        with mock.patch("dashboard.setup.discover_local", return_value=local_result()) as local, \
                mock.patch("dashboard.setup.local_networks", return_value=[]):
            response = self.client.post("/configurar/", {"consent_local": "1", **data}, follow=True)
        return response, local

    def connect(self, auth="windows", ov=None, **extra):
        self.start()
        data = {"s": "0", "auth": auth, "consent_connect": "1", **extra}
        with mock.patch("dashboard.setup.server_overview", return_value=ov or overview()) as fn:
            response = self.client.post("/configurar/ligar/", data)
        return response, fn


class TokenTests(SetupTestCase):
    def test_temporary_token_expires(self):
        token = encrypt_temporary("segredo")
        self.assertEqual(decrypt_temporary(token), "segredo")
        with mock.patch("cryptography.fernet.time.time", return_value=time.time() + 601):
            with self.assertRaises(DecryptionError):
                decrypt_temporary(token)
        with self.assertRaises(DecryptionError):
            decrypt_temporary(token[:-3] + "AAA")


class AccessAndConsentTests(SetupTestCase):
    def test_admin_only(self):
        for user in (self.operator, self.viewer):
            self.login(user)
            for url in ("/configurar/", "/configurar/servidores/", "/configurar/ligar/?s=0"):
                self.assertEqual(self.client.get(url).status_code, 403, (user, url))
        self.client.logout()
        self.assertEqual(self.client.get("/configurar/").status_code, 302)

    def test_discovery_requires_consent(self):
        with mock.patch("dashboard.setup.discover_local") as local, \
                mock.patch("dashboard.setup.local_networks", return_value=[]):
            response = self.client.post("/configurar/", {}, follow=True)
        local.assert_not_called()
        self.assertContains(response, "é preciso autorizar")

    def test_discovery_lists_servers_and_is_audited(self):
        response, local = self.start()
        local.assert_called_once()
        self.assertContains(response, "SQL Server")
        self.assertContains(response, "a correr")
        self.assertContains(response, "parado")
        self.assertContains(response, "Inicie o serviço")  # MySQL parado não pode ser usado
        event = AuditEvent.objects.get(action="SETUP_DISCOVERY_AUTHORIZED")
        self.assertEqual(event.details["scope"], ["este computador"])
        self.assertEqual(event.user, self.admin)

    def test_network_only_with_consent_and_known_network(self):
        with mock.patch("dashboard.setup.discover_local", return_value=DiscoveryResult()), \
                mock.patch("dashboard.setup.local_networks", return_value=[]), \
                mock.patch("dashboard.setup.discover_network") as network:
            response = self.client.post("/configurar/", {"consent_local": "1", "consent_network": "1",
                                                         "network": "8.8.8.0/24"}, follow=True)
        network.assert_not_called()
        self.assertContains(response, "Rede não disponível")

    def test_network_scan_with_consent(self):
        import ipaddress

        found = DiscoveryResult(servers=[DetectedServer("postgresql", "192.168.1.9", port=5432)], scanned_hosts=254)
        with mock.patch("dashboard.setup.discover_local", return_value=DiscoveryResult()), \
                mock.patch("dashboard.setup.local_networks", return_value=[ipaddress.IPv4Network("192.168.1.0/24")]), \
                mock.patch("dashboard.setup.discover_network", return_value=found) as network:
            response = self.client.post("/configurar/", {"consent_local": "1", "consent_network": "1",
                                                         "network": "192.168.1.0/24"}, follow=True)
        network.assert_called_once()
        self.assertContains(response, "192.168.1.9")
        self.assertIn("rede 192.168.1.0/24 (254 endereços)",
                      AuditEvent.objects.get(action="SETUP_DISCOVERY_AUTHORIZED").details["scope"])

    def test_connect_requires_consent(self):
        self.start()
        with mock.patch("dashboard.setup.server_overview") as fn:
            response = self.client.post("/configurar/ligar/", {"s": "0", "auth": "windows"})
        fn.assert_not_called()
        self.assertContains(response, "É preciso autorizar")

    def test_manual_server_validation(self):
        for host in ("x;DROP", "a b", "", "x" * 200):
            response = self.client.get("/configurar/ligar/", {"engine": "mssql", "host": host}, follow=True)
            self.assertContains(response, "Escolha um servidor", msg_prefix=host)
        response = self.client.get("/configurar/ligar/", {"engine": "mssql", "host": "HOTEL-SRV", "instance": "SQLEXPRESS"})
        self.assertContains(response, "HOTEL-SRV\\SQLEXPRESS")


class ConnectTests(SetupTestCase):
    def test_windows_auth_lists_databases(self):
        response, fn = self.connect()
        config = fn.call_args.args[0]
        self.assertTrue(config.uses_windows_auth)
        self.assertEqual((config.host, config.name, config.port), ("localhost", "master", None))
        self.assertContains(response, "HOTEL")
        self.assertContains(response, "provável")
        self.assertContains(response, "Criar automaticamente")
        self.assertEqual(AuditEvent.objects.get(action="SETUP_CONNECT_AUTHORIZED").details["auth"], "windows")

    def test_admin_password_never_exposed(self):
        response, fn = self.connect(auth="password", user="sa", password=ADMIN_PW)
        self.assertEqual(fn.call_args.args[0].password, ADMIN_PW)
        html = response.content.decode()
        self.assertNotIn(ADMIN_PW, html)
        token = response.context["access_token"]
        self.assertNotIn(ADMIN_PW, token)
        self.assertEqual(json.loads(decrypt_temporary(token))["password"], ADMIN_PW)
        self.assertFalse(any(ADMIN_PW in json.dumps(e.details) for e in AuditEvent.objects.all()))
        self.assertNotIn(ADMIN_PW, json.dumps(dict(self.client.session)))

    def test_password_auth_requires_user(self):
        self.start()
        with mock.patch("dashboard.setup.server_overview") as fn:
            response = self.client.post("/configurar/ligar/", {"s": "0", "auth": "password", "consent_connect": "1"})
        fn.assert_not_called()
        self.assertContains(response, "Indique o utilizador")

    def test_connection_error_shows_hint(self):
        self.start()
        with mock.patch("dashboard.setup.server_overview",
                        side_effect=ProvisioningError("Login failed for user 'sa'.")):
            response = self.client.post("/configurar/ligar/", {"s": "0", "auth": "password", "user": "sa",
                                                               "password": "x", "consent_connect": "1"})
        self.assertContains(response, "Não foi possível entrar")
        self.assertContains(response, "Utilizador ou senha incorretos")

    def test_windows_only_server_cannot_auto_create(self):
        response, _ = self.connect(ov=overview(sql_logins_allowed=False))
        self.assertContains(response, "só aceita contas do Windows")
        self.assertFalse(response.context["can_auto"])


class DatabaseAndProvisionTests(SetupTestCase):
    def access_token(self):
        response, _ = self.connect(auth="password", user="sa", password=ADMIN_PW)
        return response.context["access_token"]

    def choose(self, token, follow=False, **data):
        return self.client.post("/configurar/base/", {"access": token, "database": "HOTEL",
                                                      "known": ["HOTEL", "Outra"], "mode": "auto", **data},
                                follow=follow)

    def test_invalid_tokens_and_database(self):
        for token in ("", "lixo", encrypt_temporary("nao-e-json")[:-2] + "xx"):
            response = self.client.post("/configurar/base/", {"access": token, "database": "HOTEL", "known": ["HOTEL"]},
                                        follow=True)
            self.assertContains(response, "expiraram", msg_prefix=token[:10])
        token = self.access_token()
        response = self.choose(token, database="NaoListada", follow=True)
        self.assertContains(response, "Escolha uma base de dados")

    def test_confirm_page_shows_masked_sql(self):
        response = self.choose(self.access_token())
        plan = response.context["plan"]
        html = response.content.decode()
        self.assertIn("db_datareader", html)
        self.assertIn("gateway_leitura", html)
        self.assertNotIn(plan.password, html)
        self.assertNotIn(ADMIN_PW, html)
        self.assertEqual(json.loads(decrypt_temporary(response.context["plan_token"]))["password"], plan.password)

    def provision(self, verify=Verification(True, "ok"), consent=True):
        confirm = self.choose(self.access_token())
        data = {"access": confirm.context["access_token"], "plan": confirm.context["plan_token"]}
        if consent:
            data["consent_provision"] = "1"
        with mock.patch("dashboard.setup.apply_plan") as apply, \
                mock.patch("dashboard.setup.verify_read_only", return_value=verify) as check:
            response = self.client.post("/configurar/criar-utilizador/", data)
        return confirm, response, apply, check

    def test_provision_requires_consent(self):
        _, response, apply, _ = self.provision(consent=False)
        apply.assert_not_called()
        self.assertFalse(DataSource.objects.filter(kind="DATABASE").exists())

    def test_provision_success_creates_verified_encrypted_source(self):
        confirm, response, apply, check = self.provision()
        plan = confirm.context["plan"]
        executed = apply.call_args.args[1]
        self.assertEqual((executed.password, executed.username, executed.database), (plan.password, "gateway_leitura", "HOTEL"))
        self.assertEqual(apply.call_args.args[0].password, ADMIN_PW)  # executado com o administrador
        source = DataSource.objects.get(kind="DATABASE", company=self.a)
        self.assertRedirects(response, f"/ligacoes/{source.pk}/mapeamento/", fetch_redirect_response=False)
        self.assertEqual((source.db_engine, source.db_host, source.db_name, source.db_user),
                         ("mssql", "localhost", "HOTEL", "gateway_leitura"))
        self.assertEqual(source.connection_config().password, plan.password)
        self.assertNotIn(plan.password, source.db_password_encrypted)
        self.assertTrue(source.db_trust_server_certificate)
        self.assertEqual(check.call_args.args[0].user, "gateway_leitura")
        event = AuditEvent.objects.get(action="SETUP_READONLY_USER_CREATED")
        self.assertIn("db_datareader", event.details["sql"])
        dumped = json.dumps([e.details for e in AuditEvent.objects.all()])
        self.assertNotIn(plan.password, dumped)
        self.assertNotIn(ADMIN_PW, dumped)

    def test_failed_verification_creates_nothing(self):
        _, response, apply, _ = self.provision(verify=Verification(False, "tem escrita", ["INSERT"]))
        apply.assert_called_once()
        self.assertFalse(DataSource.objects.filter(kind="DATABASE").exists())

    def test_tampered_plan_rejected(self):
        confirm = self.choose(self.access_token())
        with mock.patch("dashboard.setup.apply_plan") as apply:
            self.client.post("/configurar/criar-utilizador/", {
                "access": confirm.context["access_token"], "plan": confirm.context["plan_token"][:-4] + "AAAA",
                "consent_provision": "1"})
        apply.assert_not_called()

    def test_existing_user_mode(self):
        token = self.access_token()
        with mock.patch("dashboard.setup.verify_read_only", return_value=Verification(False, "escreve", ["INSERT"])):
            response = self.choose(token, mode="existing", ro_user="leitor", ro_password="pw12345", follow=True)
        self.assertContains(response, "Não foi possível usar esse utilizador")
        with mock.patch("dashboard.setup.verify_read_only", return_value=Verification(True, "ok")):
            response = self.choose(token, mode="existing", ro_user="leitor", ro_password="pw12345")
        source = DataSource.objects.get(kind="DATABASE")
        self.assertEqual((source.db_user, source.connection_config().password), ("leitor", "pw12345"))

    def test_unique_codes(self):
        self.provision()
        self.provision()
        self.assertEqual(sorted(DataSource.objects.filter(kind="DATABASE").values_list("code", flat=True)),
                         ["HOTEL", "HOTEL-2"])


class SuggestionInAssistantTests(SetupTestCase):
    def test_mapping_page_prefilled_from_structure(self):
        db = fake_db(self, WITH_FK)
        source = DataSource.objects.create(company=self.a, code="ERP", name="ERP", kind="DATABASE",
                                           db_engine="sqlite", db_name=db.config.name)
        page = self.client.get(f"/ligacoes/{source.pk}/mapeamento/")
        self.assertContains(page, "Sugestão automática")
        form = page.context["form"]
        self.assertEqual(form.initial["h_document_number"], "NumeroDocumento")
        self.assertEqual(form.initial["link_column"], "FaturaRef")
        self.assertEqual(page.context["tables_form"].initial["documents_table"], "Faturas")

    def test_home_invites_setup_when_no_connections(self):
        from invoices.models import Invoice

        Invoice.objects.filter(company=self.a).delete()
        DataSource.objects.filter(company=self.a).delete()
        self.assertContains(self.client.get("/"), "Configurar automaticamente")
        self.login(self.viewer)
        self.assertNotContains(self.client.get("/"), "Configurar automaticamente")
