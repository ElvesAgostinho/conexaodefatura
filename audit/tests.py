import os
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TestCase

from audit.models import AuditEvent, IntegrationLog
from audit.services import MAX_BODY_LENGTH, audit_event, log_integration, sanitize
from companies.models import Company
from config.logging_utils import MASK
from invoices.models import Invoice


class SanitizeTests(SimpleTestCase):
    def test_sensitive_keys_masked_recursively(self):
        data = {
            "password": "p1", "Client_Secret": "s1", "access_token": "t1", "Authorization": "Bearer abc",
            "x-api-key": "k1", "privateKey": "pk", "nested": {"pwd": "p2", "items": [{"signature": "sig"}]},
            "numero": "FT A/1", "total": 10, "empty_token": "", "none_secret": None,
        }
        result = sanitize(data)
        for key in ("password", "Client_Secret", "access_token", "Authorization", "x-api-key", "privateKey"):
            self.assertEqual(result[key], MASK, key)
        self.assertEqual(result["nested"]["pwd"], MASK)
        self.assertEqual(result["nested"]["items"][0]["signature"], MASK)
        self.assertEqual((result["numero"], result["total"]), ("FT A/1", 10))
        self.assertEqual((result["empty_token"], result["none_secret"]), ("", None))
        self.assertEqual(data["password"], "p1")  # não altera o original

    def test_secrets_inside_strings_masked(self):
        self.assertNotIn("abc123", sanitize(["erro: password=abc123;"])[0])
        self.assertNotIn("tok.en", sanitize("Authorization: Bearer tok.en"))

    def test_env_secret_values_masked_anywhere(self):
        with mock.patch.dict(os.environ, {"AGT_CLIENT_SECRET": "SuperSegredoAGT"}):
            self.assertEqual(sanitize({"msg": "falhou com SuperSegredoAGT"})["msg"], f"falhou com {MASK}")

    def test_non_string_keys_and_tuples(self):
        self.assertEqual(sanitize({1: "a", "b": (1, "password=x1")}), {1: "a", "b": [1, f"password={MASK}"]})


class IntegrationLogTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="A", nif="1")

    def _invoice(self):
        return Invoice.objects.create(
            company=self.company, source_system="HOST", source_document_id="1", document_type="FT",
            document_number="FT A/1", series="A", document_date=date(2026, 1, 1), customer_name="C",
            subtotal=Decimal("100"), tax_amount=Decimal("14"), total=Decimal("114"),
        )

    def test_dict_body_masked_and_company_from_invoice(self):
        invoice = self._invoice()
        log = log_integration(
            "AGT_SEND", invoice=invoice, response_code=200,
            response_body={"token": "jwt.abc", "status": "ok"}, error_message="pwd=segredo",
        )
        log.refresh_from_db()
        self.assertEqual(log.company, self.company)
        self.assertNotIn("jwt.abc", log.response_body)
        self.assertIn('"status": "ok"', log.response_body)
        self.assertNotIn("segredo", log.error_message)

    def test_long_body_truncated(self):
        log = log_integration("AGT_SEND", company=self.company, response_body="x" * (MAX_BODY_LENGTH + 500))
        self.assertLess(len(log.response_body), MAX_BODY_LENGTH + 100)
        self.assertIn("truncado", log.response_body)

    def test_empty_values(self):
        log = log_integration("HOST_READ")
        self.assertEqual((log.response_body, log.error_message, log.request_id), ("", "", ""))
        self.assertIsNone(log.company)

    def test_invoice_deletion_keeps_log(self):
        invoice = self._invoice()
        log = log_integration("AGT_SEND", invoice=invoice)
        invoice.delete()
        log.refresh_from_db()
        self.assertIsNone(log.invoice)
        self.assertEqual(log.company, self.company)


class AuditEventTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.company = Company.objects.create(name="A", nif="1")
        self.user = get_user_model().objects.create_user("alice", password="x")

    def test_user_and_ip_from_request(self):
        request = self.factory.post("/", REMOTE_ADDR="10.0.0.5")
        request.user = self.user
        event = audit_event("INVOICE_RESEND", request=request, company=self.company, obj=self.company,
                            details={"motivo": "teste", "password": "x"})
        self.assertEqual((event.user, event.actor, event.ip_address), (self.user, "alice", "10.0.0.5"))
        self.assertEqual((event.object_type, event.object_id), ("Company", str(self.company.pk)))
        self.assertEqual(event.details, {"motivo": "teste", "password": MASK})

    def test_anonymous_request_is_system_actor(self):
        request = self.factory.get("/")
        request.user = AnonymousUser()
        event = audit_event("LOGIN_FAILED", request=request)
        self.assertIsNone(event.user)
        self.assertEqual(event.actor, "sistema")

    def test_api_key_actor_and_long_actor_truncated(self):
        self.assertEqual(audit_event("X", actor="api:ERP").actor, "api:ERP")
        self.assertEqual(len(audit_event("X", actor="a" * 300).actor), 150)

    def test_unsaved_object_and_no_request(self):
        event = audit_event("X", obj=Company(name="n", nif="9"))
        self.assertEqual((event.object_type, event.object_id, event.ip_address), ("Company", "", None))

    def test_ordering_newest_first(self):
        first = audit_event("A")
        second = audit_event("B")
        self.assertEqual(list(AuditEvent.objects.all()), [second, first])
        self.assertEqual(IntegrationLog.objects.count(), 0)
