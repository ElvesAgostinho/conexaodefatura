"""Testes das origens. A BD de origem aqui é FICTÍCIA (SQLite): não representa o HOST
nem nenhum outro sistema real; serve só para exercitar o mapeamento configurável."""

import os
import sqlite3
import tempfile
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from audit.models import IntegrationLog
from companies.models import Company
from invoices.models import Invoice, InvoiceStatus
from sources.mapping import cursor_value, rows_to_payload, validate_mapping
from sources.models import DataSource
from sources.sync import SyncError, sync_source

FAKE_SOURCE = """
CREATE TABLE Docs (Id INTEGER PRIMARY KEY, Tipo TEXT, Serie TEXT, Numero TEXT, Data TEXT,
                   Cliente TEXT, NifCliente TEXT, Base NUMERIC, Iva NUMERIC, Total NUMERIC);
CREATE TABLE Linhas (DocId INTEGER, Ord INTEGER, Descr TEXT, Qtd NUMERIC, Preco NUMERIC,
                     Taxa NUMERIC, Imposto NUMERIC, Valor NUMERIC, Isencao TEXT);
"""

MAPPING = {
    "documents_query": "SELECT * FROM Docs WHERE Id > :cursor ORDER BY Id",
    "lines_query": "SELECT * FROM Linhas WHERE DocId = :document_id ORDER BY Ord",
    "cursor_column": "Id",
    "batch_size": 100,
    "fields": {
        "source_document_id": "Id", "document_type": "Tipo", "series": "Serie", "document_number": "Numero",
        "document_date": "Data", "customer_name": "Cliente", "customer_nif": "NifCliente",
        "subtotal": "Base", "tax_amount": "Iva", "total": "Total",
    },
    "line_fields": {
        "line_number": "Ord", "description": "Descr", "quantity": "Qtd", "unit_price": "Preco",
        "tax_rate": "Taxa", "tax_amount": "Imposto", "total": "Valor", "tax_exemption_code": "Isencao",
    },
    "defaults": {"currency": "AOA"},
}


def mapping(**changes):
    data = deepcopy(MAPPING)
    data.update(changes)
    return data


class MappingValidationTests(SimpleTestCase):
    def test_valid(self):
        self.assertEqual(validate_mapping(MAPPING), [])

    def test_not_dict(self):
        self.assertTrue(validate_mapping([]))

    def test_write_queries_rejected(self):
        for key, sql in (("documents_query", "DELETE FROM Docs WHERE Id > :cursor"),
                         ("lines_query", "UPDATE Linhas SET Qtd = 0 WHERE DocId = :document_id"),
                         ("documents_query", "SELECT * FROM Docs WHERE Id > :cursor; DROP TABLE Docs")):
            with self.subTest(sql=sql):
                self.assertTrue(any(key in e for e in validate_mapping(mapping(**{key: sql}))))

    def test_parameters_required(self):
        errors = validate_mapping(mapping(documents_query="SELECT * FROM Docs", lines_query="SELECT * FROM Linhas"))
        self.assertTrue(any(":cursor" in e for e in errors))
        self.assertTrue(any(":document_id" in e for e in errors))

    def test_required_fields(self):
        data = mapping()
        del data["fields"]["total"]
        del data["line_fields"]["quantity"]
        errors = " ".join(validate_mapping(data))
        self.assertIn("total", errors)
        self.assertIn("quantity", errors)
        data["defaults"]["total"] = "0"  # um valor fixo também satisfaz
        self.assertNotIn("fields: faltam campos obrigatórios: total", " ".join(validate_mapping(data)))

    def test_source_document_id_must_be_a_column(self):
        data = mapping()
        del data["fields"]["source_document_id"]
        data["defaults"]["source_document_id"] = "1"
        self.assertTrue(any("source_document_id" in e for e in validate_mapping(data)))

    def test_unknown_keys_and_fields(self):
        errors = " ".join(validate_mapping(mapping(extra=1, fields={**MAPPING["fields"], "cor": "C"})))
        self.assertIn("extra", errors)
        self.assertIn("cor", errors)

    def test_cursor_and_batch(self):
        self.assertTrue(validate_mapping(mapping(cursor_type="uuid")))
        self.assertTrue(validate_mapping(mapping(batch_size=0)))
        self.assertTrue(validate_mapping(mapping(batch_size=5000)))
        self.assertTrue(validate_mapping(mapping(batch_size=True)))
        self.assertTrue(validate_mapping(mapping(initial_cursor="abc")))
        self.assertEqual(validate_mapping(mapping(cursor_type="datetime", initial_cursor="2026-01-01T00:00:00")), [])

    def test_cursor_value(self):
        self.assertEqual(cursor_value(MAPPING, ""), 0)
        self.assertEqual(cursor_value(MAPPING, "17"), 17)
        self.assertEqual(cursor_value(mapping(cursor_type="str"), "A-9"), "A-9")
        self.assertEqual(cursor_value(mapping(cursor_type="datetime"), "").year, 1900)

    def test_rows_to_payload_case_insensitive_and_defaults(self):
        header = {k.upper(): v for k, v in {"Id": 1, "Tipo": "FT", "Serie": "A", "Numero": "FT A/1",
                                             "Data": "2026-01-01", "Cliente": "C", "NifCliente": "",
                                             "Base": 1, "Iva": 0, "Total": 1}.items()}
        payload = rows_to_payload(MAPPING, header, [{"ord": 1, "descr": "x", "qtd": 1, "preco": 1, "taxa": 0,
                                                     "imposto": 0, "valor": 1, "isencao": "M00"}])
        self.assertEqual((payload["currency"], payload["source_document_id"]), ("AOA", 1))
        self.assertEqual(payload["lines"][0]["description"], "x")
        with self.assertRaises(KeyError):
            rows_to_payload(MAPPING, {"Id": 1}, [])


class DataSourceModelTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")

    def test_database_requires_connection_and_valid_mapping(self):
        with self.assertRaises(ValidationError) as ctx:
            DataSource(company=self.company, code="HOST", name="H", kind="DATABASE",
                       mapping={"documents_query": "DELETE FROM x"}).full_clean()
        self.assertIn("db_engine", ctx.exception.message_dict)  # modo painel: falta o sistema de BD
        self.assertIn("mapping", ctx.exception.message_dict)
        DataSource(company=self.company, code="HOST", name="H", kind="DATABASE", connection_mode="ENV", connection="default",
                   mapping=MAPPING).full_clean()
        # Sem mapeamento ainda é válido (é configurado na Fase 6 depois de estudar a BD real).
        DataSource(company=self.company, code="HOST", name="H", kind="DATABASE", connection_mode="ENV", connection="default").full_clean()

    def test_api_source_has_no_connection_or_mapping(self):
        with self.assertRaises(ValidationError) as ctx:
            DataSource(company=self.company, code="API", name="A", kind="API", connection_mode="ENV", connection="x",
                       mapping=MAPPING).full_clean()
        self.assertEqual(set(ctx.exception.message_dict), {"connection", "mapping"})

    def test_code_format_and_uniqueness(self):
        for bad in ("host", "A B", "", "X" * 31, "-A"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                DataSource(company=self.company, code=bad, name="A", kind="API").full_clean()
        DataSource.objects.create(company=self.company, code="API", name="A", kind="API")
        with self.assertRaises(ValidationError):
            DataSource(company=self.company, code="API", name="B", kind="API").full_clean()

    def test_default_api_source(self):
        a = DataSource.default_api_source(self.company)
        self.assertEqual(a, DataSource.default_api_source(self.company))
        DataSource.objects.filter(pk=a.pk).update(kind="DATABASE")
        with self.assertRaises(ValidationError):
            DataSource.default_api_source(self.company)

    def test_sync_lock(self):
        source = DataSource.objects.create(company=self.company, code="API", name="A", kind="API")
        self.assertTrue(source.acquire_sync_lock())
        self.assertFalse(source.acquire_sync_lock())
        source.release_sync_lock()
        self.assertTrue(source.acquire_sync_lock())
        DataSource.objects.filter(pk=source.pk).update(sync_locked_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(source.acquire_sync_lock())  # bloqueio expirado (processo morto)


class SyncTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "origem.sqlite3")
        self.execute(FAKE_SOURCE)
        env = mock.patch.dict(os.environ, {"HOST_H1_DB_ENGINE": "sqlite", "HOST_H1_DB_NAME": self.db_path})
        env.start()
        self.addCleanup(env.stop)
        self.company = Company.objects.create(name="Hotel", nif="5000000000")
        self.source = DataSource.objects.create(company=self.company, code="HOST", name="HOST", kind="DATABASE",
                                                connection_mode="ENV", connection="h1", mapping=deepcopy(MAPPING))

    def execute(self, script):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(script)
            conn.commit()
        finally:
            conn.close()

    def add_doc(self, doc_id, total="114.00", tax="14.00", base="100.00", lines=True, number=None):
        number = number or f"FT A/{doc_id}"
        script = (f"INSERT INTO Docs VALUES ({doc_id}, 'FT', 'A', '{number}', '2026-01-10', 'Cliente {doc_id}', "
                  f"'', {base}, {tax}, {total});")
        if lines:
            script += f"INSERT INTO Linhas VALUES ({doc_id}, 1, 'Alojamento', 2, 50, 14, 14, 100, '');"
        self.execute(script)

    def test_imports_and_advances_cursor(self):
        for i in (1, 2, 3):
            self.add_doc(i)
        result = sync_source(self.source)
        self.assertEqual((result.fetched, result.created, result.cursor), (3, 3, "3"))
        self.assertEqual(Invoice.objects.filter(status=InvoiceStatus.VALIDATED).count(), 3)
        self.source.refresh_from_db()
        self.assertEqual((self.source.sync_cursor, self.source.last_sync_status), ("3", "OK"))
        self.assertIsNone(self.source.sync_locked_until)

        self.add_doc(4)
        result = sync_source(self.source)
        self.assertEqual((result.fetched, result.created), (1, 1))  # só o novo

    def test_batch_size_respected(self):
        for i in range(1, 6):
            self.add_doc(i)
        self.source.mapping["batch_size"] = 2
        self.source.save()
        self.assertEqual(sync_source(self.source).fetched, 2)
        self.assertEqual(sync_source(self.source).fetched, 2)
        self.assertEqual(sync_source(self.source).fetched, 1)
        self.assertEqual(Invoice.objects.count(), 5)

    def test_business_error_imported_and_cursor_advances(self):
        self.add_doc(1, total="999.00")
        self.add_doc(2)
        result = sync_source(self.source)
        self.assertEqual((result.created, result.cursor), (2, "2"))
        self.assertEqual(Invoice.objects.get(source_document_id="1").status, InvoiceStatus.ERROR)

    def test_structural_error_stops_without_skipping(self):
        self.add_doc(1)
        self.add_doc(2, lines=False)  # sem linhas: não pode ser importado
        self.add_doc(3)
        result = sync_source(self.source)
        self.assertEqual((result.created, result.stopped_at, result.cursor), (1, "2", "1"))
        self.assertIn("linha", result.stop_reason)
        self.source.refresh_from_db()
        self.assertEqual((self.source.sync_cursor, self.source.last_sync_status), ("1", "PARTIAL"))
        self.assertFalse(Invoice.objects.filter(source_document_id="3").exists())
        self.assertTrue(IntegrationLog.objects.filter(action="SOURCE_READ", status="ERROR").exists())

        # Corrigido na origem, a sincronização continua do documento 2.
        self.execute("INSERT INTO Linhas VALUES (2, 1, 'Alojamento', 2, 50, 14, 14, 100, '');")
        result = sync_source(self.source)
        self.assertEqual((result.created, result.cursor, result.stop_reason), (2, "3", ""))

    def test_missing_column_stops(self):
        self.add_doc(1)
        self.source.mapping["fields"]["customer_nif"] = "ColunaQueNaoExiste"
        self.source.save()
        result = sync_source(self.source)
        self.assertIn("ColunaQueNaoExiste", result.stop_reason)
        self.assertEqual(result.cursor, "0")
        self.source.refresh_from_db()
        self.assertEqual(self.source.last_sync_status, "ERROR")

    def test_conflict_logged_and_cursor_advances(self):
        self.add_doc(1)
        sync_source(self.source)
        Invoice.objects.update(status=InvoiceStatus.CONFIRMED)
        # A origem altera um documento já comunicado e o cursor é reposto (ex.: reprocessamento).
        self.execute("UPDATE Docs SET Cliente = 'Mudou' WHERE Id = 1;")
        DataSource.objects.filter(pk=self.source.pk).update(sync_cursor="0")
        self.source.refresh_from_db()
        self.add_doc(2)
        result = sync_source(self.source)
        self.assertEqual((len(result.conflicts), result.created, result.cursor), (1, 1, "2"))
        self.assertEqual(Invoice.objects.get(source_document_id="1").customer_name, "Cliente 1")
        self.assertTrue(IntegrationLog.objects.filter(action="SOURCE_CONFLICT").exists())

    def test_rerun_after_cursor_reset_is_idempotent(self):
        self.add_doc(1)
        sync_source(self.source)
        DataSource.objects.filter(pk=self.source.pk).update(sync_cursor="")
        self.source.refresh_from_db()
        result = sync_source(self.source)
        self.assertEqual((result.duplicates, result.created), (1, 0))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_refusals(self):
        api = DataSource.objects.create(company=self.company, code="API", name="API", kind="API")
        with self.assertRaisesRegex(SyncError, "base de dados"):
            sync_source(api)
        self.source.mapping = {}
        self.source.save()
        with self.assertRaisesRegex(SyncError, "não está configurado"):
            sync_source(self.source)
        self.source.mapping = mapping(documents_query="DELETE FROM Docs WHERE Id > :cursor")
        self.source.save()
        with self.assertRaisesRegex(SyncError, "inválido"):
            sync_source(self.source)
        self.source.mapping = deepcopy(MAPPING)
        self.source.active = False
        self.source.save()
        with self.assertRaisesRegex(SyncError, "inativa"):
            sync_source(self.source)

    def test_concurrent_sync_refused(self):
        self.source.acquire_sync_lock()
        with self.assertRaisesRegex(SyncError, "Já está a decorrer"):
            sync_source(self.source)

    def test_connection_failure_reported_and_lock_released(self):
        self.source.connection = "semconfig"
        self.source.save()
        with self.assertRaisesRegex(SyncError, "HOST_SEMCONFIG_DB_ENGINE"):
            sync_source(self.source)
        self.source.refresh_from_db()
        self.assertIsNone(self.source.sync_locked_until)
        self.assertEqual(self.source.last_sync_status, "ERROR")

    def test_query_failure_masks_secrets(self):
        self.source.mapping["documents_query"] = "SELECT * FROM TabelaInexistente WHERE Id > :cursor"
        self.source.save()
        with self.assertRaisesRegex(SyncError, "TabelaInexistente"):
            sync_source(self.source)
        self.source.refresh_from_db()
        self.assertIsNone(self.source.sync_locked_until)

    def test_source_database_is_never_written(self):
        self.add_doc(1)
        before = Path(self.db_path).read_bytes()
        sync_source(self.source)
        self.assertEqual(Path(self.db_path).read_bytes(), before)

    def test_sync_sources_command(self):
        self.add_doc(1)
        out = StringIO()
        call_command("sync_sources", stdout=out)
        self.assertIn("1 novos", out.getvalue())
        call_command("sync_sources", "--company", "5000000000", "--source", "HOST", stdout=out)
        with self.assertRaisesRegex(CommandError, "Nenhuma origem"):
            call_command("sync_sources", "--company", "999", stdout=StringIO())
        with self.assertRaisesRegex(CommandError, "exige --company"):
            call_command("sync_sources", "--source", "HOST", stdout=StringIO())
        self.source.connection = "semconfig"
        self.source.save()
        with self.assertRaisesRegex(CommandError, "falharam"):
            call_command("sync_sources", stdout=StringIO(), stderr=StringIO())
