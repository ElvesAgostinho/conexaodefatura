import os
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from companies.access import companies_for_user, has_role, role_for
from companies.models import ApiKey, Company, Membership
from sources.models import DataSource
from config.logging_utils import MASK, mask_secrets
from host_connector.config import HostConfigError, HostConnectionConfig, env_prefix

User = get_user_model()
Role = Membership.Role


class NamedHostConnectionTests(SimpleTestCase):
    def test_prefix(self):
        self.assertEqual(env_prefix(None), "HOST_DB_")
        self.assertEqual(env_prefix(""), "HOST_DB_")
        self.assertEqual(env_prefix("default"), "HOST_DB_")
        self.assertEqual(env_prefix("DEFAULT"), "HOST_DB_")
        self.assertEqual(env_prefix("hotel2"), "HOST_HOTEL2_DB_")
        self.assertEqual(env_prefix("Hotel_Sul"), "HOST_HOTEL_SUL_DB_")

    def test_invalid_names_rejected(self):
        # Impede injeção de nomes de variáveis arbitrárias (ex.: ler DJANGO_SECRET_KEY).
        for bad in ("2hotel", "hotel-2", "a b", "../x", "x" * 51, "hotel;DROP", "é"):
            with self.subTest(bad=bad), self.assertRaises(HostConfigError):
                env_prefix(bad)

    def test_named_connection_reads_only_its_variables(self):
        env = {
            "HOST_DB_ENGINE": "postgresql", "HOST_DB_HOST": "srv-default", "HOST_DB_NAME": "db0",
            "HOST_DB_USER": "u0", "HOST_DB_PASSWORD": "pw-default",
            "HOST_HOTEL2_DB_ENGINE": "sqlserver", "HOST_HOTEL2_DB_HOST": r"srv2\SQLEXPRESS",
            "HOST_HOTEL2_DB_NAME": "HostDB", "HOST_HOTEL2_DB_USER": "leitura",
            "HOST_HOTEL2_DB_PASSWORD": "pw-hotel2",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            default = HostConnectionConfig.from_env()
            named = HostConnectionConfig.from_env("hotel2")
        self.assertEqual((default.engine, default.host, default.password), ("postgresql", "srv-default", "pw-default"))
        self.assertEqual((named.engine, named.host, named.name), ("mssql", r"srv2\SQLEXPRESS", "HostDB"))
        self.assertEqual(named.password, "pw-hotel2")
        self.assertIsNone(named.port)  # SQL Server: porta dinâmica
        self.assertEqual(named.env_prefix, "HOST_HOTEL2_DB_")

    def test_named_connection_does_not_fall_back_to_default(self):
        env = {"HOST_DB_ENGINE": "postgresql", "HOST_DB_HOST": "h", "HOST_DB_NAME": "d", "HOST_DB_USER": "u"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(HostConfigError, "HOST_HOTEL2_DB_ENGINE"):
                HostConnectionConfig.from_env("hotel2")

    def test_error_messages_name_the_right_variables(self):
        with mock.patch.dict(os.environ, {"HOST_X_DB_ENGINE": "mysql", "HOST_X_DB_NAME": "d"}, clear=True):
            with self.assertRaisesRegex(HostConfigError, "HOST_X_DB_HOST"):
                HostConnectionConfig.from_env("x")
        with mock.patch.dict(os.environ, {"HOST_X_DB_ENGINE": "mysql", "HOST_X_DB_PORT": "abc"}, clear=True):
            with self.assertRaisesRegex(HostConfigError, "HOST_X_DB_PORT"):
                HostConnectionConfig.from_env("x")
        with mock.patch.dict(os.environ, {"HOST_X_DB_ENGINE": "mysql", "HOST_X_DB_OPTIONS": "semigual"}, clear=True):
            with self.assertRaisesRegex(HostConfigError, "HOST_X_DB_OPTIONS"):
                HostConnectionConfig.from_env("x")
        with mock.patch.dict(os.environ, {"HOST_X_DB_ENGINE": "access"}, clear=True):
            with self.assertRaisesRegex(HostConfigError, "HOST_X_DB_URL"):
                HostConnectionConfig.from_env("x")

    def test_named_url_override(self):
        with mock.patch.dict(os.environ, {"HOST_FB_DB_URL": "firebird://u:segredo123@srv/db.fdb"}, clear=True):
            config = HostConnectionConfig.from_env("fb")
            self.assertEqual(config.engine, "firebird")
            self.assertNotIn("segredo123", str(config.describe()))


class NamedSecretMaskingTests(SimpleTestCase):
    def test_named_host_passwords_and_urls_are_masked(self):
        env = {"HOST_HOTEL2_DB_PASSWORD": "Pw!Hotel2", "HOST_SUL_DB_URL": "mssql+pyodbc://u:x@s/db?y=1"}
        with mock.patch.dict(os.environ, env, clear=True):
            masked = mask_secrets("erro Pw!Hotel2 em mssql+pyodbc://u:x@s/db?y=1")
        self.assertNotIn("Pw!Hotel2", masked)
        self.assertNotIn("mssql+pyodbc://u:x@s/db?y=1", masked)
        self.assertIn(MASK, masked)

    def test_longer_secret_masked_before_contained_one(self):
        env = {"HOST_DB_PASSWORD": "abc", "HOST_B_DB_PASSWORD": "abcdef"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(mask_secrets("x abcdef y"), f"x {MASK} y")

    def test_named_agt_secrets_are_masked(self):
        env = {"AGT_HOTEL2_CLIENT_SECRET": "agt-sec-2", "AGT_HOTEL2_PRIVATE_KEY_PASSWORD": "kpw-2"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(mask_secrets("agt-sec-2 / kpw-2"), f"{MASK} / {MASK}")

    def test_unrelated_variables_not_masked(self):
        with mock.patch.dict(os.environ, {"HOST_H2_DB_USER": "leitura", "HOST_H2_DB_NAME": "HostDB"}, clear=True):
            self.assertEqual(mask_secrets("leitura HostDB"), "leitura HostDB")


class HostCommandTargetTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "host.sqlite3")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE Documentos (Id INTEGER PRIMARY KEY, Numero TEXT)")
            conn.execute("INSERT INTO Documentos VALUES (1, 'FT A/1')")
        conn.close()
        self.env = {"HOST_H2_DB_ENGINE": "sqlite", "HOST_H2_DB_NAME": self.db_path}

    def test_host_check_with_connection(self):
        out = StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True):
            call_command("host_check", "--connection", "h2", stdout=out)
        self.assertIn(self.db_path, out.getvalue())

    def test_host_sample_with_company(self):
        company = Company.objects.create(name="Hotel 2", nif="5000000002")
        DataSource.objects.create(company=company, code="HOST", name="HOST", kind="DATABASE", connection="h2")
        DataSource.objects.create(company=company, code="API", name="API", kind="API")  # ignorada
        out = StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True):
            call_command("host_sample", "Documentos", "--company", "5000000002", stdout=out)
        self.assertIn("FT A/1", out.getvalue())

    def test_company_with_several_database_sources_needs_source(self):
        company = Company.objects.create(name="Grupo", nif="5000000004")
        DataSource.objects.create(company=company, code="HOST", name="HOST", kind="DATABASE", connection="h2")
        DataSource.objects.create(company=company, code="ERP", name="ERP", kind="DATABASE", connection="erp")
        with self.assertRaisesRegex(CommandError, "HOST, ERP|ERP, HOST"):
            call_command("host_check", "--company", "5000000004", stdout=StringIO())
        out = StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True):
            call_command("host_check", "--company", "5000000004", "--source", "HOST", stdout=out)
        self.assertIn(self.db_path, out.getvalue())
        with self.assertRaisesRegex(CommandError, "com o código X"):
            call_command("host_check", "--company", "5000000004", "--source", "X", stdout=StringIO())
        with self.assertRaisesRegex(CommandError, "exige --company"):
            call_command("host_check", "--source", "HOST", stdout=StringIO())

    def test_unknown_company_and_company_without_database_source(self):
        with self.assertRaisesRegex(CommandError, "não existe"):
            call_command("host_check", "--company", "999", stdout=StringIO())
        Company.objects.create(name="Sem HOST", nif="5000000003")
        with self.assertRaisesRegex(CommandError, "não tem origem do tipo base de dados"):
            call_command("host_check", "--company", "5000000003", stdout=StringIO())

    def test_connection_and_company_are_mutually_exclusive(self):
        with self.assertRaises(CommandError):
            call_command("host_check", "--connection", "h2", "--company", "1", stdout=StringIO())


class CompanyModelTests(TestCase):
    def test_nif_unique(self):
        Company.objects.create(name="A", nif="5000000000")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Company.objects.create(name="B", nif="5000000000")

    def test_source_connection_validation(self):
        company = Company.objects.create(name="A", nif="1")
        for good in ("default", "HOTEL2", "hotel_sul"):
            DataSource(company=company, code="HOST", name="H", kind="DATABASE", connection=good).full_clean()
        for bad in ("hotel-2", "2x", "a b"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                DataSource(company=company, code="HOST", name="H", kind="DATABASE", connection=bad).full_clean()


class AccessTests(TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="A", nif="1")
        self.b = Company.objects.create(name="B", nif="2")
        self.inactive = Company.objects.create(name="Inativa", nif="3", active=False)
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.root = User.objects.create_superuser("root", password="x")
        Membership.objects.create(user=self.alice, company=self.a, role=Role.OPERATOR)
        Membership.objects.create(user=self.alice, company=self.inactive, role=Role.ADMIN)
        Membership.objects.create(user=self.bob, company=self.b, role=Role.VIEWER)

    def test_user_sees_only_own_active_companies(self):
        self.assertEqual(list(companies_for_user(self.alice)), [self.a])
        self.assertEqual(list(companies_for_user(self.bob)), [self.b])
        self.assertEqual(set(companies_for_user(self.root)), {self.a, self.b})

    def test_anonymous_and_none(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(companies_for_user(AnonymousUser()).count(), 0)
        self.assertEqual(companies_for_user(None).count(), 0)
        self.assertIsNone(role_for(AnonymousUser(), self.a))
        self.assertIsNone(role_for(self.alice, None))

    def test_roles(self):
        self.assertEqual(role_for(self.alice, self.a), Role.OPERATOR)
        self.assertIsNone(role_for(self.alice, self.b))
        self.assertIsNone(role_for(self.alice, self.inactive))  # admin de empresa inativa não conta
        self.assertEqual(role_for(self.root, self.b), Role.ADMIN)
        self.assertIsNone(role_for(self.root, self.inactive))

    def test_has_role_hierarchy(self):
        self.assertTrue(has_role(self.alice, self.a, Role.VIEWER))
        self.assertTrue(has_role(self.alice, self.a, Role.OPERATOR))
        self.assertFalse(has_role(self.alice, self.a, Role.ADMIN))
        self.assertFalse(has_role(self.bob, self.b, Role.OPERATOR))
        self.assertFalse(has_role(self.bob, self.a, Role.VIEWER))

    def test_no_duplicate_rows_for_superuser_with_membership(self):
        Membership.objects.create(user=self.root, company=self.a, role=Role.VIEWER)
        self.assertEqual(companies_for_user(self.root).count(), 2)

    def test_membership_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(user=self.alice, company=self.a, role=Role.VIEWER)


class ApiKeyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="A", nif="1")

    def test_generate_stores_only_hash(self):
        api_key, raw = ApiKey.generate(self.company, "ERP")
        self.assertTrue(raw.startswith(f"gw_{api_key.prefix}_"))
        self.assertNotIn(raw, api_key.key_hash)
        self.assertNotEqual(api_key.key_hash, raw)
        self.assertEqual(len(api_key.key_hash), 64)
        self.assertNotIn(raw.split("_", 2)[2], str(api_key))
        self.assertEqual((api_key.source.code, api_key.source.kind), ("API", "API"))

    def test_generate_with_specific_source_and_invalid_sources(self):
        pos = DataSource.objects.create(company=self.company, code="POS", name="POS", kind="API")
        api_key, _ = ApiKey.generate(self.company, "POS", pos)
        self.assertEqual(api_key.source, pos)
        db_source = DataSource.objects.create(company=self.company, code="HOST", name="H", kind="DATABASE",
                                              connection="default")
        other = Company.objects.create(name="B", nif="2")
        other_source = DataSource.objects.create(company=other, code="API", name="API", kind="API")
        for bad in (db_source, other_source):
            with self.subTest(bad=bad.code), self.assertRaises(ValueError):
                ApiKey.generate(self.company, "x", bad)

    def test_inactive_source_blocks_key(self):
        api_key, raw = ApiKey.generate(self.company, "ERP")
        DataSource.objects.filter(pk=api_key.source_id).update(active=False)
        self.assertIsNone(ApiKey.authenticate(raw))

    def test_authenticate_ok_updates_last_used(self):
        api_key, raw = ApiKey.generate(self.company, "ERP")
        self.assertEqual(ApiKey.authenticate(raw), api_key)
        self.assertEqual(ApiKey.authenticate(f"  {raw}\n"), api_key)
        api_key.refresh_from_db()
        self.assertIsNotNone(api_key.last_used_at)

    def test_authenticate_rejects_bad_keys(self):
        api_key, raw = ApiKey.generate(self.company, "ERP")
        other_key, other_raw = ApiKey.generate(self.company, "Outro")
        tampered = raw[:-1] + ("A" if raw[-1] != "A" else "B")
        swapped = f"gw_{api_key.prefix}_{other_raw.split('_', 2)[2]}"  # prefixo de uma, segredo de outra
        for bad in ("", None, "gw", "gw_x", "xx_" + raw[3:], tampered, swapped, raw.upper(), "gw__", "gw_%_x"):
            with self.subTest(bad=bad):
                self.assertIsNone(ApiKey.authenticate(bad))
        api_key.refresh_from_db()
        self.assertIsNone(api_key.last_used_at)

    def test_revoked_and_inactive_company(self):
        api_key, raw = ApiKey.generate(self.company, "ERP")
        api_key.revoke()
        self.assertIsNone(ApiKey.authenticate(raw))
        self.assertIsNotNone(api_key.revoked_at)

        _, raw2 = ApiKey.generate(self.company, "ERP2")
        self.company.active = False
        self.company.save()
        self.assertIsNone(ApiKey.authenticate(raw2))

    def test_keys_are_unique(self):
        raws = {ApiKey.generate(self.company, f"k{i}")[1] for i in range(30)}
        self.assertEqual(len(raws), 30)
        self.assertEqual(ApiKey.objects.values("prefix").distinct().count(), 30)

    def test_create_api_key_command(self):
        out = StringIO()
        call_command("create_api_key", "--company", "1", "--name", "ERP", stdout=out)
        raw = out.getvalue().strip().splitlines()[-1]
        self.assertEqual(ApiKey.authenticate(raw).company, self.company)
        with self.assertRaisesRegex(CommandError, "não existe"):
            call_command("create_api_key", "--company", "999", "--name", "x", stdout=StringIO())
        pos = DataSource.objects.create(company=self.company, code="POS", name="POS", kind="API")
        out = StringIO()
        call_command("create_api_key", "--company", "1", "--name", "POS", "--source", "POS", stdout=out)
        self.assertEqual(ApiKey.authenticate(out.getvalue().strip().splitlines()[-1]).source, pos)
        with self.assertRaisesRegex(CommandError, "código XPTO"):
            call_command("create_api_key", "--company", "1", "--name", "x", "--source", "XPTO", stdout=StringIO())
        self.company.active = False
        self.company.save()
        with self.assertRaisesRegex(CommandError, "inativa"):
            call_command("create_api_key", "--company", "1", "--name", "x", stdout=StringIO())


class ApiKeyAdminTests(TestCase):
    def test_admin_creation_shows_key_once_and_stores_hash(self):
        company = Company.objects.create(name="A", nif="1")
        root = User.objects.create_superuser("root", password="x")
        self.client.force_login(root)
        response = self.client.post(
            "/admin/companies/apikey/add/",
            {"company": company.pk, "source": DataSource.default_api_source(company).pk, "name": "Pelo admin",
             "active": "on"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ApiKey.objects.count(), 1)
        messages = [str(m) for m in response.context["messages"]]
        raw = next(m for m in messages if "gw_" in m).split(": ", 1)[1].strip()
        self.assertEqual(ApiKey.authenticate(raw).name, "Pelo admin")
