"""Instalação e execução: .env, primeira configuração, agendador, definições de produção."""

import os
import subprocess
import sys
import tempfile
import threading
import time
from io import StringIO
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from agt.models import AgtConfiguration
from companies.models import Company, Membership
from dashboard.management.commands.run_gateway import Scheduler, run_queue_once, run_sync_once
from sources.models import DataSource

sys.path.insert(0, str(Path(settings.BASE_DIR) / "install"))
import gerar_env  # noqa: E402


def env_file_values(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#"))


class GerarEnvTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_creates_secure_env(self):
        out = self.dir / ".env"
        self.assertEqual(gerar_env.main(["--saida", str(out), "--dados", str(self.dir / "dados"), "--porta", "8123"]), 0)
        values = env_file_values(out)
        self.assertEqual(values["DJANGO_DEBUG"], "False")
        self.assertGreaterEqual(len(values["DJANGO_SECRET_KEY"]), 50)
        Fernet(values["GATEWAY_ENCRYPTION_KEY"].encode())  # chave de cifra válida
        self.assertIn("localhost", values["DJANGO_ALLOWED_HOSTS"].split(","))
        self.assertIn("http://localhost:8123", values["DJANGO_CSRF_TRUSTED_ORIGINS"])
        self.assertTrue(values["GATEWAY_DB_NAME"].endswith("gateway.sqlite3"))
        self.assertTrue((self.dir / "dados").is_dir())

    def test_keys_are_unique_per_installation(self):
        a, b = self.dir / "a.env", self.dir / "b.env"
        gerar_env.main(["--saida", str(a), "--dados", str(self.dir / "d1")])
        gerar_env.main(["--saida", str(b), "--dados", str(self.dir / "d2")])
        va, vb = env_file_values(a), env_file_values(b)
        self.assertNotEqual(va["DJANGO_SECRET_KEY"], vb["DJANGO_SECRET_KEY"])
        self.assertNotEqual(va["GATEWAY_ENCRYPTION_KEY"], vb["GATEWAY_ENCRYPTION_KEY"])

    def test_never_overwrites_existing_env(self):
        out = self.dir / ".env"
        out.write_text("DJANGO_SECRET_KEY=a-minha-chave\n", encoding="utf-8")
        gerar_env.main(["--saida", str(out), "--dados", str(self.dir / "dados")])
        self.assertEqual(out.read_text(encoding="utf-8"), "DJANGO_SECRET_KEY=a-minha-chave\n")

    def test_invalid_port(self):
        self.assertEqual(gerar_env.main(["--saida", str(self.dir / "x.env"), "--dados", str(self.dir), "--porta", "70000"]), 2)
        self.assertFalse((self.dir / "x.env").exists())


class BootstrapTests(TestCase):
    def run_cmd(self, password="Senha-Forte-2026", **kw):
        args = ["--empresa", kw.get("empresa", "Hotel X, Lda"), "--nif", kw.get("nif", "5000000123"),
                "--utilizador", kw.get("utilizador", "admin")] + kw.get("extra", [])
        with mock.patch.dict(os.environ, {"GATEWAY_ADMIN_PASSWORD": password} if password is not None else {}):
            if password is None:
                os.environ.pop("GATEWAY_ADMIN_PASSWORD", None)
            call_command("gateway_bootstrap", *args, stdout=StringIO())

    def test_creates_company_admin_and_membership(self):
        self.run_cmd()
        company = Company.objects.get(nif="5000000123")
        user = get_user_model().objects.get(username="admin")
        self.assertTrue(user.is_superuser and user.check_password("Senha-Forte-2026"))
        self.assertEqual(Membership.objects.get(user=user, company=company).role, "ADMIN")
        self.assertTrue(AgtConfiguration.objects.get(company=company).is_simulation)

    def test_idempotent_and_keeps_password(self):
        self.run_cmd()
        self.run_cmd(password="Outra-Senha-Forte-99")
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(Membership.objects.count(), 1)
        self.assertTrue(get_user_model().objects.get(username="admin").check_password("Senha-Forte-2026"))
        self.run_cmd(password="Outra-Senha-Forte-99", extra=["--redefinir-senha"])
        self.assertTrue(get_user_model().objects.get(username="admin").check_password("Outra-Senha-Forte-99"))

    def test_rejections(self):
        for kwargs, message in (({"password": "123"}, "Senha fraca"), ({"password": None}, "GATEWAY_ADMIN_PASSWORD"),
                                ({"nif": "50 00"}, "NIF inválido"), ({"empresa": " "}, "obrigatórios")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CommandError, message):
                self.run_cmd(**kwargs)
        self.assertEqual(Company.objects.count(), 0)


class SchedulerTests(TestCase):
    def test_jobs_run_on_interval_and_failures_do_not_stop(self):
        calls = {"ok": 0, "bad": 0}

        def ok():
            calls["ok"] += 1

        def bad():
            calls["bad"] += 1
            raise RuntimeError("falha simulada")

        scheduler = Scheduler([("ok", 0.05, ok), ("bad", 0.05, bad)], tick=0.01)
        with self.assertLogs("gateway.scheduler", level="ERROR"):
            scheduler.start()
            time.sleep(0.4)
            scheduler.stop()
            scheduler.join(2)
        self.assertFalse(scheduler.is_alive())
        self.assertGreaterEqual(calls["ok"], 3)
        self.assertGreaterEqual(calls["bad"], 3)
        self.assertEqual(scheduler.errors["bad"], calls["bad"])
        self.assertEqual(scheduler.errors["ok"], 0)

    def test_long_interval_runs_once_at_start(self):
        calls = []
        scheduler = Scheduler([("x", 3600, lambda: calls.append(1))], tick=0.01)
        scheduler.start()
        time.sleep(0.2)
        scheduler.stop()
        scheduler.join(2)
        self.assertEqual(len(calls), 1)

    def test_runs_immediately_right_after_windows_boot(self):
        # Regressão: com o Windows ligado há poucos segundos, o relógio monótono é pequeno e a
        # primeira execução ficava adiada até ao fim do intervalo.
        calls = []
        scheduler = Scheduler([("x", 300, lambda: calls.append(1))], tick=0.01)
        with mock.patch("dashboard.management.commands.run_gateway.time.monotonic", return_value=10.0):
            scheduler.start()
            time.sleep(0.2)
            scheduler.stop()
            scheduler.join(2)
        self.assertEqual(len(calls), 1)

    def test_sync_job_skips_unmapped_and_reports(self):
        company = Company.objects.create(name="A", nif="1")
        DataSource.objects.create(company=company, code="SEM", name="sem mapeamento", kind="DATABASE",
                                  db_engine="mssql", db_host="h", db_name="d")
        with mock.patch("sources.sync.sync_source") as sync:
            self.assertEqual(run_sync_once(), 0)
        sync.assert_not_called()
        run_queue_once()  # fila vazia: não falha

    def test_command_argument_validation(self):
        for args in (["--port", "0"], ["--sync-minutes", "0"], ["--queue-minutes", "-1"]):
            with self.subTest(args=args), self.assertRaises(CommandError):
                call_command("run_gateway", *args)


class ProductionSettingsTests(SimpleTestCase):
    def settings_in_subprocess(self, **env):
        code = ("import django,os;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';django.setup();"
                "from django.conf import settings as s;"
                "print(s.DEBUG, s.SESSION_COOKIE_SECURE, getattr(s,'SECURE_SSL_REDIRECT',False), "
                "'whitenoise.middleware.WhiteNoiseMiddleware' in s.MIDDLEWARE)")
        full_env = {**os.environ, "DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "x" * 60, **env}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=full_env,
                             cwd=settings.BASE_DIR)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.split()

    def test_https_default_is_secure(self):
        self.assertEqual(self.settings_in_subprocess(DJANGO_HTTPS="True"), ["False", "True", "True", "True"])

    def test_internal_network_http_mode(self):
        self.assertEqual(self.settings_in_subprocess(DJANGO_HTTPS="False"), ["False", "False", "False", "True"])
