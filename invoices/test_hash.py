"""Hash / assinatura da fatura: transportado tal como vem do sistema de faturação."""

import json
from datetime import date

from django.test import SimpleTestCase, TestCase

from agt.client import build_payload
from companies.models import ApiKey, Company
from invoices.canonical import CanonicalError, parse_document, validate_document
from invoices.models import Invoice, InvoiceStatus
from invoices.services import DocumentConflict, import_document
from invoices.testing import document, numbered
from sources.autodetect import HEADER_SYNONYMS, match_columns
from sources.models import DataSource

# Valor fictício com o aspeto de uma assinatura RSA em base64 (não é de nenhum sistema real).
FAKE_HASH = "mYJEE1n6mwVbq4E+1tHyW0gM9pl3e3DqQ0jy3ZF5F0kPj0ZC7xQ2h0S2sR1Vq9eHbF3dQyTz0L8u5A6p4W2kMw=="


class CanonicalHashTests(SimpleTestCase):
    def test_optional_and_parsed(self):
        self.assertEqual(parse_document(document()).document_hash, "")
        doc = parse_document(document(document_hash=f"  {FAKE_HASH} ", hash_control="1"))
        self.assertEqual((doc.document_hash, doc.hash_control), (FAKE_HASH, "1"))
        self.assertEqual(validate_document(doc, today=date(2026, 2, 1)), [])

    def test_invalid_values(self):
        for label, data in {
            "espaço no meio": document(document_hash="abc def"),
            "quebra de linha": document(document_hash="abc\ndef"),
            "demasiado longo": document(document_hash="A" * 1025),
            "controlo longo": document(hash_control="1" * 71),
            "controlo com espaço": document(hash_control="versão 1"),
        }.items():
            with self.subTest(label), self.assertRaises(CanonicalError):
                parse_document(data)
        parse_document(document(document_hash="A" * 1024))  # no limite: aceite

    def test_hash_is_part_of_fingerprint(self):
        a = parse_document(document(document_hash=FAKE_HASH)).fingerprint()
        b = parse_document(document(document_hash=FAKE_HASH[:-4] + "AAA=")).fingerprint()
        self.assertNotEqual(a, b)

    def test_autodetect_columns(self):
        chosen = match_columns(["Id", "Hash", "HashControl", "Total"], HEADER_SYNONYMS)
        self.assertEqual((chosen["document_hash"], chosen["hash_control"]), ("Hash", "HashControl"))
        chosen = match_columns(["Assinatura", "VersaoChave"], HEADER_SYNONYMS)
        self.assertEqual((chosen["document_hash"], chosen["hash_control"]), ("Assinatura", "VersaoChave"))


class ImportHashTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")
        self.source = DataSource.objects.create(company=self.company, code="API", name="API", kind="API")

    def test_stored_and_sent_to_agt_unchanged(self):
        invoice = import_document(self.source, numbered(1, document_hash=FAKE_HASH, hash_control="1")).invoice
        invoice.refresh_from_db()
        self.assertEqual((invoice.document_hash, invoice.hash_control), (FAKE_HASH, "1"))
        payload = build_payload(invoice)
        self.assertEqual((payload["document_hash"], payload["hash_control"]), (FAKE_HASH, "1"))

    def test_changed_hash_after_communication_is_conflict(self):
        invoice = import_document(self.source, numbered(1, document_hash=FAKE_HASH)).invoice
        Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.CONFIRMED)
        with self.assertRaises(DocumentConflict):
            import_document(self.source, numbered(1, document_hash="OUTRO" + FAKE_HASH[5:]))
        self.assertTrue(import_document(self.source, numbered(1, document_hash=FAKE_HASH)).duplicate)

    def test_api_accepts_hash(self):
        _, raw = ApiKey.generate(self.company, "ERP", self.source)
        response = self.client.post("/api/v1/documents/", json.dumps(numbered(7, document_hash=FAKE_HASH,
                                                                              hash_control="2")),
                                    content_type="application/json", HTTP_AUTHORIZATION=f"Api-Key {raw}")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Invoice.objects.get(source_document_id="7").document_hash, FAKE_HASH)

    def test_detail_page_shows_hash(self):
        from django.contrib.auth import get_user_model

        from companies.models import Membership

        invoice = import_document(self.source, numbered(1, document_hash=FAKE_HASH, hash_control="1")).invoice
        user = get_user_model().objects.create_user("u", password="x")
        Membership.objects.create(user=user, company=self.company, role="VIEWER")
        self.client.force_login(user)
        page = self.client.get(f"/faturas/{invoice.pk}/")
        self.assertContains(page, FAKE_HASH[:30])
        self.assertContains(page, "chave 1")
