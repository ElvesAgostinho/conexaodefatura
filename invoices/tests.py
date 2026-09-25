from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from companies.models import Company
from invoices.models import STATUS_GROUPS, Invoice, InvoiceItem, InvoiceStatus


def make_invoice(company, **overrides):
    data = dict(
        company=company, source_system="HOST", source_document_id="1", document_type="FT",
        document_number="FT A/1", series="A", document_date=date(2026, 1, 1), customer_name="Cliente",
        subtotal=Decimal("100.00"), tax_amount=Decimal("14.00"), total=Decimal("114.00"),
    )
    data.update(overrides)
    return Invoice.objects.create(**data)


class InvoiceConstraintTests(TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="A", nif="1")
        self.b = Company.objects.create(name="B", nif="2")

    def test_defaults(self):
        invoice = make_invoice(self.a)
        self.assertEqual((invoice.status, invoice.currency, invoice.send_attempts), (InvoiceStatus.PENDING, "AOA", 0))
        self.assertEqual((invoice.validation_errors, invoice.agt_response), ([], {}))

    def test_same_source_twice_rejected(self):
        make_invoice(self.a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_invoice(self.a, document_number="FT A/2")

    def test_same_fiscal_number_from_other_source_rejected(self):
        make_invoice(self.a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_invoice(self.a, source_system="API", source_document_id="xyz")

    def test_same_numbers_allowed_in_other_company(self):
        make_invoice(self.a)
        make_invoice(self.b)
        self.assertEqual(Invoice.objects.count(), 2)

    def test_same_number_other_type_or_series_allowed(self):
        make_invoice(self.a)
        make_invoice(self.a, source_document_id="2", document_type="NC")
        make_invoice(self.a, source_document_id="3", series="B")
        self.assertEqual(Invoice.objects.filter(company=self.a).count(), 3)

    def test_company_with_invoices_cannot_be_deleted(self):
        make_invoice(self.a)
        with self.assertRaises(ProtectedError):
            self.a.delete()

    def test_decimal_precision_preserved_at_max(self):
        invoice = make_invoice(self.a, subtotal=Decimal("9999999999999.99"), tax_amount=Decimal("0.01"),
                               total=Decimal("1234567890123.45"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.subtotal, Decimal("9999999999999.99"))
        self.assertEqual(invoice.tax_amount, Decimal("0.01"))
        self.assertEqual(invoice.total, Decimal("1234567890123.45"))

    def test_amount_above_max_fails_validation(self):
        from django.core.exceptions import ValidationError

        invoice = Invoice(company=self.a, source_system="HOST", source_document_id="9", document_type="FT",
                          document_number="FT A/9", series="A", document_date=date(2026, 1, 1), customer_name="C",
                          subtotal=Decimal("10000000000000.00"), tax_amount=0, total=0)
        with self.assertRaises(ValidationError) as ctx:
            invoice.full_clean()
        self.assertIn("subtotal", ctx.exception.message_dict)


class InvoiceItemTests(TestCase):
    def setUp(self):
        self.invoice = make_invoice(Company.objects.create(name="A", nif="1"))

    def _item(self, line, **overrides):
        data = dict(invoice=self.invoice, line_number=line, description="Alojamento", quantity=Decimal("2.5"),
                    unit_price=Decimal("40.1234"), tax_rate=Decimal("14"), tax_amount=Decimal("14.04"),
                    total=Decimal("100.31"))
        data.update(overrides)
        return InvoiceItem.objects.create(**data)

    def test_line_number_unique_and_ordering(self):
        self._item(2)
        self._item(1)
        self.assertEqual([i.line_number for i in self.invoice.items.all()], [1, 2])
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._item(1)

    def test_items_deleted_with_invoice(self):
        self._item(1)
        self.invoice.delete()
        self.assertEqual(InvoiceItem.objects.count(), 0)

    def test_four_decimal_places_kept(self):
        item = self._item(1)
        item.refresh_from_db()
        self.assertEqual((item.quantity, item.unit_price, item.discount), (Decimal("2.5000"), Decimal("40.1234"), 0))


class StatusGroupTests(TestCase):
    def test_every_status_in_exactly_one_group(self):
        grouped = [status for statuses in STATUS_GROUPS.values() for status in statuses]
        self.assertEqual(sorted(grouped), sorted(InvoiceStatus.values))
        self.assertEqual(len(grouped), len(set(grouped)))
