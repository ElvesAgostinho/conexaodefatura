"""Área de ajuda: acesso, conteúdo, imagens e ligações a partir das páginas de ligações."""

import re
import xml.etree.ElementTree as ET

from django.contrib.staticfiles import finders

from sources.models import DataSource

from .tests import DashboardTestCase

PAGES = ["/ajuda/", "/ajuda/base-de-dados/", "/ajuda/api/"]


class HelpTests(DashboardTestCase):
    def test_login_required(self):
        for url in PAGES:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith("/entrar/"))

    def test_every_role_can_read(self):
        for user in (self.viewer, self.operator, self.admin):
            self.login(user)
            for url in PAGES:
                self.assertEqual(self.client.get(url).status_code, 200, (user, url))

    def test_menu_link(self):
        self.login(self.viewer)
        self.assertContains(self.client.get("/"), 'href="/ajuda/"')

    def test_all_images_exist_and_svgs_are_valid(self):
        self.login(self.viewer)
        for url in PAGES:
            html = self.client.get(url).content.decode()
            for src in re.findall(r'<img src="/static/([^"]+)"', html):
                path = finders.find(src)
                self.assertIsNotNone(path, f"{url}: imagem em falta {src}")
                if src.endswith(".svg"):
                    ET.parse(path)  # XML válido (uma ilustração partida lançaria erro)
            self.assertNotRegex(html, r'<img(?![^>]*alt=)[^>]*>', f"{url}: imagem sem texto alternativo")

    def test_database_guide_content(self):
        self.login(self.viewer)
        page = self.client.get("/ajuda/base-de-dados/")
        for text in ("Descobrir o nome do servidor", "db_datareader", "Gerar script e senha", "Testar ligação",
                     "Problemas comuns", "Login failed for user", "PostgreSQL", "MySQL", "Oracle",
                     "SQL Server and Windows Authentication mode"):
            self.assertContains(page, text)
        # O gerador cria a senha no navegador: o HTML nunca traz uma senha pronta.
        self.assertContains(page, "crypto.getRandomValues")
        self.assertNotRegex(page.content.decode(), r"PASSWORD = N'Gw-[A-Za-z0-9]{10,}")

    def test_api_guide_shows_real_endpoint(self):
        self.login(self.viewer)
        page = self.client.get("/ajuda/api/")
        self.assertContains(page, "http://testserver/api/v1/documents/")
        self.assertContains(page, "http://testserver/api/v1/ping/")
        for text in ("chave de API", "uma única vez", "201", "409", "Invoke-RestMethod"):
            self.assertContains(page, text)

    def test_guides_linked_from_connection_pages(self):
        db = DataSource.objects.create(company=self.a, code="HOST", name="HOST", kind="DATABASE",
                                       db_engine="mssql", db_host="srv", db_name="d")
        self.login(self.admin)
        self.assertContains(self.client.get("/ligacoes/"), 'href="/ajuda/"')
        self.assertContains(self.client.get("/ligacoes/nova/base-de-dados/"), 'href="/ajuda/base-de-dados/"')
        self.assertContains(self.client.get("/ligacoes/nova/api/"), 'href="/ajuda/api/"')
        self.assertContains(self.client.get(f"/ligacoes/{db.pk}/"), 'href="/ajuda/base-de-dados/"')
        self.assertContains(self.client.get(f"/ligacoes/{self.src_a.pk}/"), 'href="/ajuda/api/"')
