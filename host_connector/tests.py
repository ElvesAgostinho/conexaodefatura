import os
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from sqlalchemy import text

from config.logging_utils import mask_secrets
from host_connector.config import HostConfigError, HostConnectionConfig
from host_connector.connection import HostDatabase
from host_connector.inspection import SchemaInspector
from host_connector.sql_guard import ReadOnlyViolation, assert_read_only

# Estrutura fictícia usada SÓ nos testes; não representa o HOST real.
FAKE_SCHEMA = """
CREATE TABLE Clientes (Id INTEGER PRIMARY KEY, Nome TEXT NOT NULL, NIF TEXT);
CREATE TABLE Documentos (
    Id INTEGER PRIMARY KEY, Serie TEXT, Numero INTEGER, ClienteId INTEGER REFERENCES Clientes(Id),
    Total NUMERIC, Hash TEXT
);
CREATE TABLE DocumentoLinhas (
    Id INTEGER PRIMARY KEY, DocumentoId INTEGER REFERENCES Documentos(Id),
    Descricao TEXT, Quantidade NUMERIC, PrecoUnit NUMERIC
);
CREATE UNIQUE INDEX ux_doc ON Documentos (Serie, Numero);
CREATE VIEW vwDocumentos AS SELECT Id, Serie, Numero FROM Documentos;
INSERT INTO Clientes VALUES (1, 'Empresa Teste', '5000000000');
INSERT INTO Documentos VALUES (1, 'A', 1, 1, 100, 'abc');
"""


class SqlGuardTests(SimpleTestCase):
    def test_allows_select_and_cte(self):
        assert_read_only("SELECT * FROM Documentos WHERE Id = :id")
        assert_read_only("WITH x AS (SELECT 1 AS a) SELECT a FROM x;")
        assert_read_only("select REPLACE(Nome, 'a', 'b') from Clientes")

    def test_keywords_inside_literals_and_identifiers_are_ignored(self):
        assert_read_only("SELECT [Delete], \"Update\" FROM T WHERE Obs = 'DROP TABLE x; --'")

    def test_column_names_containing_keywords_are_allowed(self):
        assert_read_only("SELECT IsDeleted, UpdatedAt, CreatedBy FROM Documentos")

    def test_blocks_writes(self):
        for sql in (
            "UPDATE Documentos SET Total = 0",
            "DELETE FROM Documentos",
            "INSERT INTO T VALUES (1)",
            "DROP TABLE Documentos",
            "TRUNCATE TABLE Documentos",
            "ALTER TABLE Documentos ADD x INT",
            "EXEC sp_who",
            "SELECT * INTO Copia FROM Documentos",
            "SELECT * FROM Documentos FOR UPDATE",
            "WITH x AS (SELECT 1) DELETE FROM Documentos",
        ):
            with self.subTest(sql=sql), self.assertRaises(ReadOnlyViolation):
                assert_read_only(sql)

    def test_blocks_multiple_statements_and_comment_tricks(self):
        for sql in (
            "SELECT 1; DROP TABLE Documentos",
            "SELECT 1; SELECT 2",
            "/* x */ UPDATE Documentos SET Total = 0",
            "SELECT 1 /* unterminated",
            "",
        ):
            with self.subTest(sql=sql), self.assertRaises(ReadOnlyViolation):
                assert_read_only(sql)


class MaskSecretsTests(SimpleTestCase):
    def test_masks_passwords_in_urls_and_connection_strings(self):
        self.assertNotIn("S3gredo!", mask_secrets("mssql+pyodbc://sa:S3gredo!@srv/db"))
        self.assertNotIn("S3gredo", mask_secrets("DRIVER={x};UID=sa;PWD=S3gredo;"))
        self.assertNotIn("abc.def", mask_secrets("Authorization: Bearer abc.def"))

    def test_masks_explicit_secret(self):
        self.assertEqual(mask_secrets("erro com xyz123", extra_secrets=("xyz123",)), "erro com ***")


class HostConfigTests(SimpleTestCase):
    def _env(self, **values):
        base = {key: "" for key in os.environ if key.startswith("HOST_DB_")}
        base.update(values)
        return mock.patch.dict(os.environ, base)

    def test_mssql_url_is_read_only_intent_and_hides_password(self):
        with self._env(HOST_DB_ENGINE="mssql", HOST_DB_HOST="srv", HOST_DB_NAME="HOSTDB",
                       HOST_DB_USER="leitor", HOST_DB_PASSWORD="Senha#123"):
            config = HostConnectionConfig.from_env()
        url = config.sqlalchemy_url()
        self.assertEqual(url.drivername, "mssql+pyodbc")
        self.assertIsNone(url.port)  # instâncias com nome usam porta dinâmica
        self.assertEqual(url.query["ApplicationIntent"], "ReadOnly")
        described = str(config.describe())
        self.assertNotIn("Senha#123", described)
        self.assertNotIn("Senha#123", repr(config))

    def test_mssql_without_user_uses_windows_auth(self):
        with self._env(HOST_DB_ENGINE="sqlserver", HOST_DB_HOST="srv", HOST_DB_NAME="HOSTDB"):
            config = HostConnectionConfig.from_env()
        self.assertTrue(config.uses_windows_auth)
        self.assertEqual(config.sqlalchemy_url().query["Trusted_Connection"], "yes")

    def test_other_engines(self):
        cases = {"postgresql": ("postgresql+psycopg", 5432), "mysql": ("mysql+pymysql", 3306),
                 "oracle": ("oracle+oracledb", 1521)}
        for engine, (driver, port) in cases.items():
            with self.subTest(engine=engine), self._env(HOST_DB_ENGINE=engine, HOST_DB_HOST="h",
                                                        HOST_DB_NAME="n", HOST_DB_USER="u"):
                url = HostConnectionConfig.from_env().sqlalchemy_url()
                self.assertEqual((url.drivername, url.port), (driver, port))

    def test_custom_url_for_other_dbms(self):
        with self._env(HOST_DB_URL="firebird+fdb://u:segredo@h:3050/C:/dados/host.fdb"):
            config = HostConnectionConfig.from_env()
        self.assertEqual(config.engine, "firebird")
        self.assertNotIn("segredo", str(config.describe()))

    def test_missing_or_invalid_config(self):
        with self._env(), self.assertRaises(HostConfigError):
            HostConnectionConfig.from_env()
        with self._env(HOST_DB_ENGINE="access"), self.assertRaises(HostConfigError):
            HostConnectionConfig.from_env()
        with self._env(HOST_DB_ENGINE="postgresql", HOST_DB_HOST="h", HOST_DB_NAME="n"), \
                self.assertRaises(HostConfigError):
            HostConnectionConfig.from_env()


class SqliteHostTestCase(SimpleTestCase):
    """Usa um ficheiro SQLite temporário como BD de HOST de teste."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "host_teste.db"
        with sqlite3.connect(self.path) as conn:
            conn.executescript(FAKE_SCHEMA)
        conn.close()
        self.env = mock.patch.dict(os.environ, {
            **{key: "" for key in os.environ if key.startswith("HOST_DB_")},
            "HOST_DB_ENGINE": "sqlite", "HOST_DB_NAME": str(self.path),
            "HOST_REPORTS_DIR": self.tmp.name,
        })
        self.env.start()
        self.db = HostDatabase(HostConnectionConfig.from_env())

    def tearDown(self):
        self.db.dispose()
        self.env.stop()
        self.tmp.cleanup()


class HostDatabaseTests(SqliteHostTestCase):
    def test_connection_ok(self):
        result = self.db.test_connection()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.message, "CONEXÃO OK")

    def test_fetch_all_is_parameterized(self):
        rows = self.db.fetch_all("SELECT Nome FROM Clientes WHERE NIF = :nif", {"nif": "5000000000"})
        self.assertEqual(rows, [{"Nome": "Empresa Teste"}])
        self.assertEqual(self.db.fetch_all("SELECT Nome FROM Clientes WHERE NIF = :nif", {"nif": "' OR 1=1 --"}), [])

    def test_fetch_all_rejects_writes(self):
        with self.assertRaises(ReadOnlyViolation):
            self.db.fetch_all("DELETE FROM Clientes")

    def test_database_level_read_only_even_bypassing_guard(self):
        with self.assertRaises(Exception), self.db.connect() as conn:
            conn.execute(text("DELETE FROM Clientes"))
        self.assertEqual(len(self.db.fetch_all("SELECT * FROM Clientes")), 1)

    def test_connection_error_is_reported(self):
        broken = HostDatabase(HostConnectionConfig(engine="sqlite", name=str(self.path) + ".nao_existe"))
        result = broken.test_connection()
        self.assertFalse(result.ok)


class SchemaInspectorTests(SqliteHostTestCase):
    def test_reports_real_structure(self):
        report = SchemaInspector(self.db).run()
        objects = {obj["name"]: obj for schema in report["schemas"] for obj in (*schema["tables"], *schema["views"])}
        self.assertEqual(set(objects), {"Clientes", "Documentos", "DocumentoLinhas", "vwDocumentos"})
        self.assertEqual(objects["vwDocumentos"]["kind"], "view")

        linhas = objects["DocumentoLinhas"]
        self.assertEqual(linhas["primary_key"], ["Id"])
        self.assertEqual(linhas["foreign_keys"][0]["referred_table"], "Documentos")
        self.assertIn({"name": "ux_doc", "columns": ["Serie", "Numero"], "unique": True},
                      objects["Documentos"]["indexes"])

        header = [c["object"] for c in report["candidates"]["cabecalho_fatura"]]
        self.assertEqual(header[0], "main.Documentos")
        self.assertIn("main.DocumentoLinhas", [c["object"] for c in report["candidates"]["linhas_fatura"]])


class KeywordHeuristicTests(SimpleTestCase):
    def test_short_keywords_only_match_word_starts(self):
        from host_connector.inspection import _keyword_hits

        self.assertEqual(_keyword_hits("sysalerts", ("sale",)), [])
        self.assertEqual(_keyword_hits("Ativa", ("iva",)), [])
        self.assertEqual(_keyword_hits("ClienteNIF", ("nif",)), ["nif"])
        self.assertEqual(_keyword_hits("TBL_FACTURAS", ("fact",)), ["fact"])
        self.assertEqual(_keyword_hits("tblfacturas", ("factur",)), ["factur"])


class CommandTests(SqliteHostTestCase):
    def test_host_check(self):
        out = StringIO()
        call_command("host_check", stdout=out)
        self.assertIn("CONEXÃO OK", out.getvalue())

    def test_host_check_error(self):
        with mock.patch.dict(os.environ, {"HOST_DB_NAME": str(self.path) + ".x"}), self.assertRaises(CommandError):
            call_command("host_check", stdout=StringIO())

    def test_host_inspect_writes_reports(self):
        out = StringIO()
        call_command("host_inspect", "--output", self.tmp.name, stdout=out)
        reports = list(Path(self.tmp.name).glob("host_schema_*"))
        self.assertEqual({p.suffix for p in reports}, {".json", ".md"})
        self.assertIn("Documentos", next(p for p in reports if p.suffix == ".md").read_text(encoding="utf-8"))

    def test_host_sample(self):
        out = StringIO()
        call_command("host_sample", "Documentos", "--order-by", "Id", "--desc", stdout=out)
        self.assertIn("abc", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("host_sample", "TabelaInventada", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("host_sample", "Documentos", "--order-by", "ColunaInventada", stdout=StringIO())
