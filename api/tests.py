import json

from django.core.cache import cache
from django.test import TestCase, override_settings

from audit.models import AuditEvent, IntegrationLog
from companies.models import ApiKey, Company
from invoices.models import Invoice, InvoiceStatus
from invoices.services import import_document
from invoices.testing import document, numbered
from sources.models import DataSource

URL = "/api/v1/documents/"


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Hotel", nif="5000000000")
        self.erp = DataSource.objects.create(company=self.company, code="ERP", name="ERP", kind="API")
        self.pos = DataSource.objects.create(company=self.company, code="POS", name="POS", kind="API")
        self.key, self.raw = ApiKey.generate(self.company, "ERP", self.erp)
        _, self.pos_raw = ApiKey.generate(self.company, "POS", self.pos)
        other = Company.objects.create(name="Outro", nif="5000000001")
        _, self.other_raw = ApiKey.generate(other, "Outro")

    def post(self, data, raw=None, **extra):
        body = data if isinstance(data, (str, bytes)) else json.dumps(data)
        return self.client.post(URL, body, content_type=extra.pop("content_type", "application/json"),
                                HTTP_AUTHORIZATION=f"Api-Key {raw or self.raw}", **extra)

    def get(self, url, raw=None):
        return self.client.get(url, HTTP_AUTHORIZATION=f"Api-Key {raw or self.raw}")


class AuthenticationTests(ApiTestCase):
    def test_missing_or_bad_credentials(self):
        self.assertEqual(self.client.get("/api/v1/ping/").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/ping/", HTTP_AUTHORIZATION="Api-Key gw_x_y").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/ping/", HTTP_AUTHORIZATION=f"Bearer {self.raw}").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/ping/", HTTP_AUTHORIZATION="Api-Key").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/ping/").headers["WWW-Authenticate"], "Api-Key")

    def test_both_header_styles(self):
        r = self.client.get("/api/v1/ping/", HTTP_AUTHORIZATION=f"api-key {self.raw}")
        self.assertEqual(r.json()["source"]["code"], "ERP")
        r = self.client.get("/api/v1/ping/", HTTP_X_API_KEY=self.raw)
        self.assertEqual(r.json()["company"]["nif"], "5000000000")

    def test_revoked_inactive_source_inactive_company(self):
        self.key.revoke()
        self.assertEqual(self.get("/api/v1/ping/").status_code, 401)
        self.assertEqual(self.get("/api/v1/ping/", self.pos_raw).status_code, 200)
        DataSource.objects.filter(pk=self.pos.pk).update(active=False)
        self.assertEqual(self.get("/api/v1/ping/", self.pos_raw).status_code, 401)
        Company.objects.filter(nif="5000000001").update(active=False)
        self.assertEqual(self.get("/api/v1/ping/", self.other_raw).status_code, 401)

    def test_session_login_is_not_enough(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(get_user_model().objects.create_superuser("root", password="x"))
        self.assertEqual(self.client.get("/api/v1/ping/").status_code, 401)
        self.assertEqual(self.client.post(URL, "{}", content_type="application/json").status_code, 401)


class SubmitTests(ApiTestCase):
    def test_create_duplicate_update_flow(self):
        r = self.post(document(total="999.00"))
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body["result"], body["status"], body["source"]), ("created", "ERROR", "ERP"))
        self.assertTrue(body["validation_errors"])

        r = self.post(document(total="999.00"))
        self.assertEqual((r.status_code, r.json()["result"]), (200, "duplicate"))

        r = self.post(document())
        self.assertEqual((r.status_code, r.json()["result"], r.json()["status"]), (200, "updated", "VALIDATED"))
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action__startswith="API_DOCUMENT").count(), 2)
        self.assertEqual(AuditEvent.objects.first().actor, "api:ERP")

    def test_structural_errors_400_and_nothing_stored(self):
        r = self.post({"document_date": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("source_document_id: obrigatório.", r.json()["errors"])
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertTrue(IntegrationLog.objects.filter(action="API_RECEIVE", status="INVALID").exists())

    def test_non_json_and_bad_json(self):
        self.assertEqual(self.post("não é json").status_code, 400)
        self.assertEqual(self.post("[1, 2]").status_code, 400)
        self.assertEqual(self.post("a=1", content_type="application/x-www-form-urlencoded").status_code, 415)

    def test_conflict_with_other_source_409_without_leaking_document(self):
        self.post(document())
        r = self.post(document(source_document_id="pos-1"), raw=self.pos_raw)
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("document", r.json())

    def test_conflict_after_communication_409_with_status(self):
        self.post(document())
        Invoice.objects.update(status=InvoiceStatus.CONFIRMED)
        r = self.post(document(customer_name="Alterado"))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["document"]["status"], "CONFIRMED")

    def test_documents_are_assigned_to_the_key_source(self):
        self.post(numbered(1))
        self.post(numbered(2), raw=self.pos_raw)
        self.assertEqual(Invoice.objects.get(source_document_id="1").source, self.erp)
        self.assertEqual(Invoice.objects.get(source_document_id="2").source, self.pos)

    def test_other_company_isolated(self):
        self.post(document())
        r = self.post(document(), raw=self.other_raw)
        self.assertEqual(r.status_code, 201)  # outra empresa: numeração independente
        self.assertEqual(Invoice.objects.count(), 2)

    def test_client_cannot_choose_source_or_status(self):
        r = self.post(document(source="HOST", status="CONFIRMED"))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("desconhecidos" in e for e in r.json()["errors"]))

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1000)
    def test_body_size_limit(self):
        big = document()
        big["lines"][0]["description"] = "x" * 400
        big["lines"][1]["description"] = "y" * 400
        self.assertEqual(self.post(big).status_code, 413)

    def test_csrf_not_required_for_api_key(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        r = client.post(URL, json.dumps(document()), content_type="application/json",
                        HTTP_AUTHORIZATION=f"Api-Key {self.raw}")
        self.assertEqual(r.status_code, 201)

    def test_methods(self):
        r = self.client.put(URL, "{}", content_type="application/json", HTTP_AUTHORIZATION=f"Api-Key {self.raw}")
        self.assertEqual(r.status_code, 405)


class ReadTests(ApiTestCase):
    def test_detail_scoped_to_source(self):
        import_document(self.erp, numbered(1))
        r = self.get(f"{URL}1/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["document_number"], "FT A2026/1")
        self.assertEqual(self.get(f"{URL}1/", self.pos_raw).status_code, 404)
        self.assertEqual(self.get(f"{URL}1/", self.other_raw).status_code, 404)
        self.assertEqual(self.get(f"{URL}nao-existe/").status_code, 404)

    def test_list_filter_and_pagination(self):
        for n in range(1, 106):
            import_document(self.erp, numbered(n))
        import_document(self.erp, numbered(200, total="1.00"))
        import_document(self.pos, numbered(300))
        r = self.get(URL).json()
        self.assertEqual((r["count"], r["pages"], len(r["results"])), (106, 2, 100))
        self.assertEqual(len(self.get(f"{URL}?page=2").json()["results"]), 6)
        self.assertEqual(self.get(f"{URL}?page=3").status_code, 404)
        self.assertEqual(self.get(f"{URL}?page=abc").status_code, 400)
        self.assertEqual(self.get(f"{URL}?status=ERROR").json()["count"], 1)
        self.assertEqual(self.get(f"{URL}?status=XPTO").status_code, 400)
        self.assertEqual(self.get(URL, self.pos_raw).json()["count"], 1)

    def test_status_payload_has_no_internal_data(self):
        invoice = import_document(self.erp, document()).invoice
        body = self.get(f"{URL}{invoice.source_document_id}/").json()
        self.assertNotIn("source_data", body)
        self.assertNotIn("agt_response", body)
        self.assertEqual(set(body["agt"]), {"request_id", "message", "simulated", "sent_at", "confirmed_at"})


@override_settings(REST_FRAMEWORK={
    "DEFAULT_AUTHENTICATION_CLASSES": [], "UNAUTHENTICATED_USER": None,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_THROTTLE_RATES": {"api_key": "3/min"},
})
class ThrottleTests(ApiTestCase):
    def test_rate_limit_per_key(self):
        from api.throttling import ApiKeyRateThrottle

        ApiKeyRateThrottle.THROTTLE_RATES = {"api_key": "3/min"}
        try:
            codes = [self.get("/api/v1/ping/").status_code for _ in range(4)]
            self.assertEqual(codes, [200, 200, 200, 429])
            self.assertEqual(self.get("/api/v1/ping/", self.pos_raw).status_code, 200)  # outra chave, outro limite
        finally:
            from rest_framework.settings import api_settings

            ApiKeyRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES
