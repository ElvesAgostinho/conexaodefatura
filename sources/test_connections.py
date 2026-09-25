"""Ligação configurada no painel, catálogo, geração de SQL e assistente de mapeamento.

A BD de origem usada aqui é FICTÍCIA (SQLite): não representa nenhum sistema real.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from companies.models import Company
from config.crypto import DecryptionError, decrypt, encrypt
from config.logging_utils import MASK, mask_secrets
from host_connector.catalog import (CatalogError, TableRef, build_select, list_tables, quote_table, resolve_table,
                                    sample_rows, table_columns)
from host_connector.config import HostConfigError, HostConnectionConfig
from host_connector.connection import HostDatabase
from sources.assistant import AssistantChoices, build_mapping, preview
from sources.mapping import validate_mapping
from sources.models import DataSource
from sources.sync import sync_source

FAKE = """
CREATE TABLE "Doc Cab" (Id INTEGER PRIMARY KEY, Tipo TEXT, Serie TEXT, Numero TEXT, Data TEXT, Cliente TEXT,
                        Base NUMERIC, Iva NUMERIC, Total NUMERIC, Segredo TEXT);
CREATE TABLE DocLin (DocId INTEGER, Ord INTEGER, Descr TEXT, Qtd NUMERIC, Preco NUMERIC, Taxa NUMERIC,
                     Imposto NUMERIC, Valor NUMERIC);
CREATE VIEW vwDocs AS SELECT Id, Numero FROM "Doc Cab";
INSERT INTO "Doc Cab" VALUES (1, 'FT', 'A', 'FT A/1', '2026-01-10', 'Cliente 1', 100, 14, 114, 'x');
INSERT INTO "Doc Cab" VALUES (2, 'FT', 'A', 'FT A/2', '2026-01-11', 'Cliente 2', 100, 14, 999, 'y');
INSERT INTO DocLin VALUES (1, 1, 'Alojamento', 2, 50, 14, 14, 100);
INSERT INTO DocLin VALUES (2, 1, 'Alojamento', 2, 50, 14, 14, 100);
"""


def make_fake_db(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = str(Path(tmp.name) / "origem.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(FAKE)
    conn.commit()
    conn.close()
    return path


def choices(**overrides):
    data = dict(
        documents_table=TableRef(None, "Doc Cab"), lines_table=TableRef(None, "DocLin"),
        header_columns={"source_document_id": "Id", "document_type": "Tipo", "series": "Serie",
                        "document_number": "Numero", "document_date": "Data", "customer_name": "Cliente",
                        "subtotal": "Base", "tax_amount": "Iva", "total": "Total"},
        header_defaults={"currency": "AOA"},
        line_columns={"line_number": "Ord", "description": "Descr", "quantity": "Qtd", "unit_price": "Preco",
                      "tax_rate": "Taxa", "tax_amount": "Imposto", "total": "Valor"},
        line_defaults={}, cursor_column="Id", cursor_type="int", link_column="DocId",
    )
    data.update(overrides)
    return AssistantChoices(**data)


class CryptoTests(SimpleTestCase):
    def test_roundtrip_and_randomized(self):
        token = encrypt("Senha#Forte 123")
        self.assertNotIn("Senha", token)
        self.assertEqual(decrypt(token), "Senha#Forte 123")
        self.assertNotEqual(encrypt("x"), encrypt("x"))  # IV aleatório
        self.assertEqual((encrypt(""), decrypt("")), ("", ""))

    def test_tampered_or_wrong_key(self):
        token = encrypt("segredo")
        with self.assertRaises(DecryptionError):
            decrypt(token[:-4] + "AAAA")
        with override_settings(SECRET_KEY="outra-chave-completamente-diferente"):
            with self.assertRaises(DecryptionError):
                decrypt(token)

    def test_explicit_key_and_masked(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"GATEWAY_ENCRYPTION_KEY": key}):
            token = encrypt("abc")
            self.assertEqual(decrypt(token), "abc")
            self.assertEqual(mask_secrets(f"key {key}"), f"key {MASK}")
        with self.assertRaises(DecryptionError):
            decrypt(token)  # sem a chave explícita já não decifra


class FromValuesTests(SimpleTestCase):
    def test_sqlserver_named_instance_and_windows_auth(self):
        config = HostConnectionConfig.from_values(engine="mssql", host=r"SRV\SQLEXPRESS", name="HostDB")
        self.assertTrue(config.uses_windows_auth)
        self.assertIsNone(config.port)
        url = config.sqlalchemy_url()
        self.assertEqual(url.query["ApplicationIntent"], "ReadOnly")
        self.assertEqual(url.query["Trusted_Connection"], "yes")

    def test_defaults_and_messages(self):
        config = HostConnectionConfig.from_values(engine="postgres", host="h", name="d", user="u", password="p")
        self.assertEqual((config.engine, config.port), ("postgresql", 5432))
        self.assertNotIn("p@", config.describe()["location"])
        for kwargs, expected in (({"engine": "mysql", "name": "d", "user": "u"}, "servidor"),
                                 ({"engine": "mysql", "host": "h", "user": "u"}, "nome da base de dados"),
                                 ({"engine": "mysql", "host": "h", "name": "d"}, "utilizador"),
                                 ({"engine": "access"}, "não suportado"),
                                 ({"engine": "mysql", "host": "h", "name": "d", "user": "u", "options": "x"}, "Opções")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(HostConfigError, expected):
                HostConnectionConfig.from_values(**kwargs)

    def test_url_engine(self):
        config = HostConnectionConfig.from_values(engine="url", url="firebird://u:pw123@srv/db.fdb")
        self.assertEqual(config.engine, "firebird")
        self.assertNotIn("pw123", str(config.describe()))


class PanelConnectionModelTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")

    def source(self, **kwargs):
        data = dict(company=self.company, code="HOST", name="HOST", kind="DATABASE")
        data.update(kwargs)
        return DataSource(**data)

    def test_password_stored_encrypted_only(self):
        source = self.source(db_engine="postgresql", db_host="h", db_name="d", db_user="u")
        source.set_db_password("SenhaMuitoSecreta!")
        source.save()
        raw = DataSource.objects.filter(pk=source.pk).values_list("db_password_encrypted", flat=True).get()
        self.assertNotIn("SenhaMuitoSecreta", raw)
        self.assertEqual(source.connection_config().password, "SenhaMuitoSecreta!")
        self.assertNotIn("SenhaMuitoSecreta", repr(source.connection_config()))

    def test_validation_per_engine(self):
        cases = {
            "no engine": ({}, "db_engine"),
            "no host": ({"db_engine": "mysql", "db_name": "d", "db_user": "u"}, "db_host"),
            "no user": ({"db_engine": "oracle", "db_host": "h", "db_name": "d"}, "db_user"),
            "sqlite no path": ({"db_engine": "sqlite"}, "db_name"),
            "url missing": ({"db_engine": "url"}, "db_engine"),
            "bad options": ({"db_engine": "mysql", "db_host": "h", "db_name": "d", "db_user": "u",
                             "db_options": "semigual"}, "db_options"),
        }
        for label, (kwargs, field) in cases.items():
            with self.subTest(label), self.assertRaises(ValidationError) as ctx:
                self.source(**kwargs).full_clean()
            self.assertIn(field, ctx.exception.message_dict)
        self.source(db_engine="mssql", db_host="srv", db_name="d").full_clean()  # Windows auth

    def test_env_mode(self):
        source = self.source(connection_mode="ENV", connection="h9")
        source.full_clean()
        with mock.patch.dict(os.environ, {"HOST_H9_DB_ENGINE": "mysql", "HOST_H9_DB_HOST": "h",
                                          "HOST_H9_DB_NAME": "d", "HOST_H9_DB_USER": "u"}, clear=True):
            self.assertEqual(source.connection_config().engine, "mysql")
        with self.assertRaises(ValidationError):
            self.source(connection_mode="ENV").full_clean()

    def test_config_errors(self):
        with self.assertRaisesRegex(HostConfigError, "sistema de base de dados"):
            self.source().connection_config()
        source = self.source(db_engine="postgresql", db_host="h", db_name="d", db_user="u",
                             db_password_encrypted="lixo")
        with self.assertRaisesRegex(HostConfigError, "decifrar"):
            source.connection_config()
        with self.assertRaises(HostConfigError):
            DataSource(company=self.company, code="API", name="API", kind="API").connection_config()

    def test_summary(self):
        self.assertIn("SQL Server · srv:1433 · HostDB",
                      self.source(db_engine="mssql", db_host="srv", db_port=1433, db_name="HostDB").connection_summary())
        self.assertEqual(self.source(connection_mode="ENV", connection="default").connection_summary(), ".env: default")


class CatalogTests(TestCase):
    def setUp(self):
        self.path = make_fake_db(self)
        self.db = HostDatabase(HostConnectionConfig.from_values(engine="sqlite", name=self.path))
        self.addCleanup(self.db.dispose)

    def test_list_and_resolve(self):
        tables = list_tables(self.db)
        names = [(t.qualified, t.kind) for t in tables]
        self.assertIn(("Doc Cab", "tabela"), names)
        self.assertIn(("vwDocs", "vista"), names)
        self.assertEqual(resolve_table(self.db, "DocLin", tables).name, "DocLin")
        with self.assertRaises(CatalogError):
            resolve_table(self.db, "DocLin; DROP TABLE DocLin", tables)

    def test_columns_and_sample(self):
        ref = TableRef(None, "Doc Cab")
        cols = table_columns(self.db, ref)
        self.assertEqual((cols[0]["name"], cols[0]["type"], cols[0]["pk"]), ("Id", "INTEGER", True))
        names, rows = sample_rows(self.db, ref)
        self.assertEqual(names[3], "Numero")
        self.assertEqual(len(rows), 2)

    def test_quoting_and_build_select(self):
        ref = TableRef(None, "Doc Cab")
        self.assertEqual(quote_table(self.db, ref), '"Doc Cab"')
        available = {c["name"] for c in table_columns(self.db, ref)}
        sql = build_select(self.db, ref, ["Numero", "Total"], "Id", ">", "cursor", "Id", available)
        self.assertEqual(sql, 'SELECT "Numero", "Total", "Id" FROM "Doc Cab" WHERE "Id" > :cursor ORDER BY "Id"')
        self.assertEqual(len(self.db.fetch_all(sql, {"cursor": 0})), 2)
        for bad_cols, op in ((["Numero; DROP TABLE x"], ">"), (["Numero"], "<>")):
            with self.subTest(cols=bad_cols, op=op), self.assertRaises(CatalogError):
                build_select(self.db, ref, bad_cols, "Id", op, "cursor", None, available)

    def test_quote_injection_attempt_in_identifier(self):
        # Mesmo um nome real com aspas é citado corretamente (escape duplo).
        conn = sqlite3.connect(self.path)
        conn.execute('CREATE TABLE "Ma""l" (Id INTEGER)')
        conn.commit()
        conn.close()
        self.db.dispose()
        ref = resolve_table(self.db, 'Ma"l')
        sql = build_select(self.db, ref, ["Id"], "Id", ">", "cursor", None, {"Id"})
        self.assertIn('"Ma""l"', sql)
        self.assertEqual(self.db.fetch_all(sql, {"cursor": 0}), [])


class AssistantTests(TestCase):
    def setUp(self):
        self.path = make_fake_db(self)
        self.db = HostDatabase(HostConnectionConfig.from_values(engine="sqlite", name=self.path))
        self.addCleanup(self.db.dispose)
        self.doc_cols = {c["name"] for c in table_columns(self.db, TableRef(None, "Doc Cab"))}
        self.line_cols = {c["name"] for c in table_columns(self.db, TableRef(None, "DocLin"))}

    def test_build_mapping_is_valid_and_selects_only_mapped_columns(self):
        mapping = build_mapping(self.db, choices(), self.doc_cols, self.line_cols)
        self.assertEqual(validate_mapping(mapping), [])
        self.assertNotIn("Segredo", mapping["documents_query"])  # colunas não mapeadas não são lidas
        self.assertIn('ORDER BY "Ord"', mapping["lines_query"])
        self.assertEqual(mapping["defaults"], {"currency": "AOA"})
        self.assertEqual(mapping["assistant"]["link_column"], "DocId")

    def test_default_ignored_when_column_mapped(self):
        mapping = build_mapping(self.db, choices(header_defaults={"currency": "AOA", "series": "Z"}),
                                self.doc_cols, self.line_cols)
        self.assertNotIn("series", mapping["defaults"])

    def test_unknown_column_rejected(self):
        with self.assertRaises(CatalogError):
            build_mapping(self.db, choices(cursor_column="NaoExiste"), self.doc_cols, self.line_cols)

    def test_preview_does_not_import(self):
        mapping = build_mapping(self.db, choices(), self.doc_cols, self.line_cols)
        items = preview(self.db, mapping)
        self.assertEqual([i.ok for i in items], [True, False])  # o 2.º tem total errado
        self.assertTrue(any("Total" in e for e in items[1].errors))
        from invoices.models import Invoice

        self.assertEqual(Invoice.objects.count(), 0)

    def test_preview_with_bad_mapping(self):
        items = preview(self.db, {"documents_query": "DELETE FROM x"})
        self.assertFalse(items[0].ok)

    def test_end_to_end_panel_source_sync(self):
        company = Company.objects.create(name="Hotel", nif="5000000000")
        mapping = build_mapping(self.db, choices(), self.doc_cols, self.line_cols)
        source = DataSource.objects.create(company=company, code="ERP", name="ERP", kind="DATABASE",
                                           db_engine="sqlite", db_name=self.path, mapping=mapping)
        result = sync_source(source)
        self.assertEqual((result.created, result.cursor), (2, "2"))
