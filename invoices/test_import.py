import threading
from unittest import mock
from datetime import date, datetime
from decimal import Decimal

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from agt.models import AgtConfiguration
from audit.models import IntegrationLog
from companies.models import Company
from invoices.canonical import CanonicalError, parse_document, validate_document
from invoices.models import Invoice, InvoiceStatus
from invoices.services import DocumentConflict, import_document, revalidate
from invoices.testing import document, numbered
from sources.models import DataSource


class ParseTests(SimpleTestCase):
    def test_valid_document(self):
        doc = parse_document(document())
        self.assertEqual(doc.document_date, date(2026, 1, 15))
        self.assertEqual(doc.total, Decimal("164.00"))
        self.assertEqual(len(doc.lines), 2)
        self.assertEqual(doc.lines[1].discount, Decimal("0.00"))
        self.assertEqual(validate_document(doc, today=date(2026, 2, 1)), [])

    def test_accepts_native_types_from_databases(self):
        data = document(document_date=datetime(2026, 1, 15, 10, 30), subtotal=Decimal("150"), tax_amount=14,
                        total=164.0, source_document_id=1001, document_type="ft")
        data["lines"][0].update(quantity=2, unit_price=50.0, tax_rate=Decimal("14.0000"))
        doc = parse_document(data)
        self.assertEqual((doc.source_document_id, doc.document_type), ("1001", "FT"))
        self.assertEqual(doc.subtotal, Decimal("150.00"))

    def test_float_does_not_leak_binary_error(self):
        data = document()
        data["lines"][0]["unit_price"] = 0.1
        self.assertEqual(parse_document(data).lines[0].unit_price, Decimal("0.1000"))

    def test_comma_decimal_separator(self):
        self.assertEqual(parse_document(document(total="164,00")).total, Decimal("164.00"))

    def test_collects_all_structural_errors(self):
        with self.assertRaises(CanonicalError) as ctx:
            parse_document({"document_date": "31/01/2026", "subtotal": "abc", "extra": 1, "lines": []})
        text = " ".join(ctx.exception.errors)
        for expected in ("source_document_id: obrigatório", "document_date: data inválida",
                         "subtotal: valor numérico inválido", "Campos desconhecidos: extra", "pelo menos uma linha"):
            self.assertIn(expected, text)

    def test_invalid_values(self):
        cases = {
            "too many decimals": document(total="164.001"),
            "too big": document(total="10000000000000.00"),
            "nan": document(total="NaN"),
            "infinity": document(total="Infinity"),
            "bool": document(total=True),
            "bad currency": document(currency="KZ"),
            "bad type": document(document_type="F T"),
            "long number": document(document_number="x" * 61),
            "bad nif": document(customer_nif="50 00"),
            "not dict": ["x"],
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(CanonicalError):
                parse_document(data)

    def test_line_errors(self):
        for label, change in {
            "missing": {"description": None},
            "unknown": {"cor": "azul"},
            "bad line number": {"line_number": "x"},
            "zero line number": {"line_number": 0},
            "rate > 100": {"tax_rate": "101"},
            "qty 5 decimals": {"quantity": "1.00001"},
        }.items():
            data = document()
            data["lines"][0].update(change)
            with self.subTest(label), self.assertRaises(CanonicalError):
                parse_document(data)
        with self.assertRaises(CanonicalError):
            parse_document(document(lines=["x"]))
        with self.assertRaises(CanonicalError):
            parse_document(document(lines=[document()["lines"][0]] * 1001))

    def test_line_number_defaults_to_position(self):
        data = document()
        for line in data["lines"]:
            del line["line_number"]
        self.assertEqual([l.line_number for l in parse_document(data).lines], [1, 2])

    def test_fingerprint_stable_and_sensitive(self):
        a = parse_document(document()).fingerprint()
        self.assertEqual(a, parse_document(document(total=164)).fingerprint())  # mesmo valor, outro tipo
        self.assertNotEqual(a, parse_document(document(customer_name="Outro")).fingerprint())


class BusinessValidationTests(SimpleTestCase):
    def errors(self, data, today=date(2026, 2, 1)):
        return validate_document(parse_document(data), today=today)

    def test_totals_mismatch(self):
        self.assertTrue(any("Subtotal" in e for e in self.errors(document(subtotal="151.00", total="165.00"))))
        self.assertTrue(any("Total" in e for e in self.errors(document(total="165.00"))))
        self.assertTrue(any("Imposto" in e for e in self.errors(document(tax_amount="20.00", total="170.00"))))

    def test_one_cent_tolerance(self):
        self.assertEqual(self.errors(document(total="164.01")), [])
        self.assertNotEqual(self.errors(document(total="164.02")), [])

    def test_line_rules(self):
        cases = {
            "line total": ({"total": "99.00"}, "quantidade × preço"),
            "line tax": ({"tax_amount": "10.00"}, "total × taxa"),
            "negative": ({"quantity": "-2", "total": "-100.00", "tax_amount": "-14.00"}, "negativo"),
            "zero qty": ({"quantity": "0", "total": "0", "tax_amount": "0"}, "quantidade zero"),
        }
        for label, (change, expected) in cases.items():
            data = document()
            data["lines"][0].update(change)
            with self.subTest(label):
                self.assertTrue(any(expected in e for e in self.errors(data)), self.errors(data))

    def test_zero_rate_requires_exemption_code(self):
        data = document()
        data["lines"][1]["tax_exemption_code"] = ""
        self.assertTrue(any("isenção" in e for e in self.errors(data)))

    def test_discount_considered(self):
        data = document(subtotal="140.00", tax_amount="12.60", total="152.60")
        data["lines"][0].update(discount="10.00", total="90.00", tax_amount="12.60")
        self.assertEqual(self.errors(data), [])

    def test_duplicate_line_numbers(self):
        data = document()
        data["lines"][1]["line_number"] = 1
        self.assertIn("Números de linha repetidos.", self.errors(data))

    def test_future_date(self):
        self.assertTrue(self.errors(document(document_date="2026-02-03")))
        self.assertEqual(self.errors(document(document_date="2026-02-02")), [])  # tolerância de fuso


class ImportTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")
        self.host = DataSource.objects.create(company=self.company, code="HOST", name="HOST", kind="DATABASE",
                                              connection="default")
        self.api = DataSource.objects.create(company=self.company, code="API", name="API", kind="API")

    def test_create_valid(self):
        result = import_document(self.host, document())
        invoice = result.invoice
        self.assertTrue(result.created)
        self.assertEqual((invoice.status, invoice.source, invoice.source_system), (InvoiceStatus.VALIDATED, self.host, "HOST"))
        self.assertEqual(invoice.items.count(), 2)
        self.assertIsNotNone(invoice.validated_at)
        self.assertEqual(invoice.source_data["customer_name"], "Cliente Teste, Lda")
        self.assertTrue(IntegrationLog.objects.filter(action="IMPORT", invoice=invoice).exists())

    def test_business_errors_stored_as_error(self):
        invoice = import_document(self.host, document(total="999.00")).invoice
        self.assertEqual(invoice.status, InvoiceStatus.ERROR)
        self.assertTrue(invoice.validation_errors)
        self.assertIsNone(invoice.validated_at)

    def test_structural_error_stores_nothing(self):
        with self.assertRaises(CanonicalError):
            import_document(self.host, document(document_date="ontem"))
        self.assertEqual(Invoice.objects.count(), 0)

    def test_identical_reimport_is_duplicate(self):
        first = import_document(self.host, document()).invoice
        again = import_document(self.host, document())
        self.assertTrue(again.duplicate)
        self.assertEqual(again.invoice.pk, first.pk)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_changed_before_sending_is_updated(self):
        first = import_document(self.host, document(total="999.00")).invoice
        self.assertEqual(first.status, InvoiceStatus.ERROR)
        fixed = import_document(self.host, document())
        self.assertTrue(fixed.updated)
        self.assertEqual(fixed.invoice.pk, first.pk)
        self.assertEqual(fixed.invoice.status, InvoiceStatus.VALIDATED)
        self.assertEqual(fixed.invoice.validation_errors, [])

    def test_line_removed_on_update(self):
        import_document(self.host, document())
        data = document(subtotal="100.00", total="114.00")
        data["lines"] = data["lines"][:1]
        invoice = import_document(self.host, data).invoice
        self.assertEqual(invoice.items.count(), 1)

    def test_changed_after_communication_is_conflict(self):
        invoice = import_document(self.host, document()).invoice
        for status in (InvoiceStatus.SENDING, InvoiceStatus.SENT, InvoiceStatus.CONFIRMED, InvoiceStatus.REJECTED):
            Invoice.objects.filter(pk=invoice.pk).update(status=status)
            with self.subTest(status), self.assertRaises(DocumentConflict):
                import_document(self.host, document(customer_name="Alterado"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.customer_name, "Cliente Teste, Lda")

    def test_error_after_failed_send_is_not_replaced(self):
        invoice = import_document(self.host, document()).invoice
        Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.ERROR, send_attempts=3)
        with self.assertRaises(DocumentConflict):
            import_document(self.host, document(customer_name="Alterado"))

    def test_same_fiscal_number_from_other_source_is_conflict(self):
        import_document(self.host, document())
        with self.assertRaisesRegex(DocumentConflict, "origem HOST"):
            import_document(self.api, document(source_document_id="outro-id"))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_same_source_id_in_different_sources_are_independent(self):
        import_document(self.host, numbered(1))
        import_document(self.api, numbered(2, source_document_id="1"))
        self.assertEqual(Invoice.objects.count(), 2)

    def test_update_cannot_steal_fiscal_number(self):
        import_document(self.host, numbered(1))
        import_document(self.host, numbered(2))
        with self.assertRaises(DocumentConflict):
            import_document(self.host, numbered(2, document_number="FT A2026/1"))

    def test_other_company_same_numbers_ok(self):
        other = Company.objects.create(name="Outro", nif="5000000001")
        other_source = DataSource.objects.create(company=other, code="HOST", name="H", kind="DATABASE",
                                                 connection="default")
        import_document(self.host, document())
        import_document(other_source, document())
        self.assertEqual(Invoice.objects.count(), 2)

    def test_secrets_in_payload_masked_in_source_data(self):
        # Um sistema de origem mal configurado que envia um campo sensível não o vê guardado.
        data = document()
        data["lines"][0]["description"] = "Alojamento password=abc123"
        invoice = import_document(self.host, data).invoice
        self.assertNotIn("abc123", str(invoice.source_data))

    def test_auto_send_enqueues(self):
        AgtConfiguration.objects.create(company=self.company, auto_send=True)
        self.assertEqual(import_document(self.host, numbered(1)).invoice.status, InvoiceStatus.QUEUED)
        self.assertEqual(import_document(self.host, numbered(2, total="1.00")).invoice.status, InvoiceStatus.ERROR)

    def test_race_with_other_import_resolved_as_duplicate(self):
        # Simula a corrida: a verificação não vê o registo que outro processo acabou de gravar.
        first = import_document(self.host, document()).invoice
        with mock.patch.object(Invoice.objects, "select_for_update", return_value=Invoice.objects.none()),                 mock.patch("invoices.services._check_fiscal_number_free"):
            result = import_document(self.host, document())
            self.assertTrue(result.duplicate)
            self.assertEqual(result.invoice.pk, first.pk)
            with self.assertRaises(DocumentConflict):
                import_document(self.host, document(customer_name="Diferente"))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_revalidate(self):
        invoice = import_document(self.host, document()).invoice
        invoice.items.filter(line_number=1).update(total=Decimal("1.00"))
        self.assertTrue(revalidate(invoice))
        self.assertEqual(invoice.status, InvoiceStatus.ERROR)
        Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.CONFIRMED)
        invoice.refresh_from_db()
        with self.assertRaises(DocumentConflict):
            revalidate(invoice)


class ConcurrentImportTests(TransactionTestCase):
    """Duas importações simultâneas do mesmo documento nunca criam dois registos."""

    def test_parallel_import_same_document(self):
        if connection.vendor == "sqlite":
            self.skipTest("A BD de testes SQLite (em memória) não é partilhada entre threads; "
                          "corre em PostgreSQL/SQL Server/MySQL/Oracle.")
        company = Company.objects.create(name="Hotel", nif="5000000000")
        source = DataSource.objects.create(company=company, code="HOST", name="HOST", kind="DATABASE",
                                           connection="default")
        outcomes, errors = [], []

        def worker():
            try:
                outcomes.append(import_document(source, document()))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(sum(o.created for o in outcomes), 1)
        for exc in errors:  # SQLite pode recusar escrita concorrente ("database is locked")
            self.assertIn("locked", str(exc))
