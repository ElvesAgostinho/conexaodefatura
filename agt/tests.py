import os
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from agt.client import AgtResult, OfficialAgtClient, Outcome, SimulatedAgtClient, build_payload, client_for
from agt.models import AgtConfiguration
from agt.queue import QueueError, enqueue, process_queue, requeue_stuck, stuck_sending
from audit.models import IntegrationLog
from companies.models import Company
from invoices.models import Invoice, InvoiceStatus
from invoices.services import import_document
from invoices.testing import document, numbered
from sources.models import DataSource


class AgtConfigurationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")

    def test_defaults_are_safe(self):
        config = AgtConfiguration.for_company(self.company)
        self.assertEqual(config.environment, AgtConfiguration.Environment.SIMULATION)
        self.assertFalse(config.auto_send)
        self.assertEqual(AgtConfiguration.for_company(self.company).pk, config.pk)
        config.full_clean()

    def test_real_environment_requires_only_url(self):
        # O Gateway não emite faturas: o certificado é do software de faturação do cliente e é opcional.
        config = AgtConfiguration(company=self.company, environment="PRODUCTION")
        with self.assertRaises(ValidationError) as ctx:
            config.full_clean()
        self.assertEqual(set(ctx.exception.message_dict), {"base_url"})
        AgtConfiguration(company=self.company, environment="PRODUCTION",
                         base_url="https://exemplo.invalid/agt").full_clean()

    def test_missing_list_speaks_of_client_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            missing = AgtConfiguration(company=self.company, base_url="https://x.invalid").missing_for_real_sending()
        self.assertFalse(any("certificado" in m for m in missing))
        self.assertTrue(any("credencial do cliente AGT_CLIENT_SECRET" in m for m in missing))

    def test_https_only(self):
        config = AgtConfiguration(company=self.company, base_url="http://exemplo.invalid/api")
        with self.assertRaises(ValidationError) as ctx:
            config.full_clean()
        self.assertIn("base_url", ctx.exception.message_dict)

    def test_limits(self):
        for field, value in (("timeout_seconds", 0), ("timeout_seconds", 301), ("max_attempts", 0),
                             ("max_attempts", 21), ("retry_delay_seconds", 5)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                AgtConfiguration(company=self.company, **{field: value}).full_clean()
        with self.assertRaises(ValidationError):
            AgtConfiguration(company=self.company, credentials_prefix="x-1").full_clean()

    def test_secrets_come_from_env_and_are_never_exposed(self):
        config = AgtConfiguration(company=self.company, credentials_prefix="hotel2")
        env = {"AGT_HOTEL2_CLIENT_SECRET": "s3cr3t", "AGT_CLIENT_SECRET": "outro"}
        with mock.patch.dict(os.environ, env, clear=True):
            status = config.secrets_status()
            self.assertEqual(config.get_secret("CLIENT_SECRET"), "s3cr3t")
        self.assertEqual(status, {"AGT_HOTEL2_CLIENT_SECRET": True, "AGT_HOTEL2_PRIVATE_KEY": False,
                                  "AGT_HOTEL2_PRIVATE_KEY_PASSWORD": False})
        self.assertNotIn("s3cr3t", str(status))
        field_names = {f.name for f in AgtConfiguration._meta.get_fields()}
        self.assertFalse(field_names & {"client_secret", "private_key", "password"})

    def test_default_prefix(self):
        self.assertEqual(AgtConfiguration(company=self.company).secret_env_name("CLIENT_SECRET"), "AGT_CLIENT_SECRET")

    def test_missing_for_real_sending(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            missing = AgtConfiguration(company=self.company).missing_for_real_sending()
        self.assertTrue(any("endereço" in m for m in missing))
        self.assertTrue(any("AGT_CLIENT_SECRET" in m for m in missing))

    def test_client_selection(self):
        self.assertIsInstance(client_for(AgtConfiguration(environment="SIMULATION")), SimulatedAgtClient)
        self.assertIsInstance(client_for(AgtConfiguration(environment="TEST")), OfficialAgtClient)
        self.assertIsInstance(client_for(AgtConfiguration(environment="PRODUCTION")), OfficialAgtClient)


class QueueTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")
        self.source = DataSource.objects.create(company=self.company, code="API", name="API", kind="API")
        self.config = AgtConfiguration.for_company(self.company)

    def new(self, n=1, **overrides):
        return import_document(self.source, numbered(n, **overrides)).invoice

    def fake_client(self, *results):
        results = list(results)

        class Fake(SimulatedAgtClient):
            simulated = False

            def send(inner, invoice):
                result = results.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

        return mock.patch("agt.queue.client_for", lambda config: Fake(config))

    def test_enqueue_rules(self):
        invoice = self.new()
        self.assertTrue(enqueue(invoice))
        self.assertEqual(invoice.status, InvoiceStatus.QUEUED)
        with self.assertRaises(QueueError):
            enqueue(invoice)  # já em fila
        bad = self.new(2, total="1.00")
        with self.assertRaisesRegex(QueueError, "erros de validação"):
            enqueue(bad, force=True)

    def test_enqueue_is_race_safe(self):
        invoice = self.new()
        stale = Invoice.objects.get(pk=invoice.pk)
        self.assertTrue(enqueue(invoice))
        self.assertFalse(enqueue(stale))  # cópia desatualizada: o UPDATE condicional não apanha nada

    def test_simulation_confirms(self):
        invoice = self.new()
        enqueue(invoice)
        stats = process_queue()
        invoice.refresh_from_db()
        self.assertEqual((stats.processed, stats.confirmed), (1, 1))
        self.assertEqual(invoice.status, InvoiceStatus.CONFIRMED)
        self.assertTrue(invoice.simulated)
        self.assertTrue(invoice.agt_request_id.startswith("SIM-"))
        self.assertEqual(invoice.send_attempts, 1)
        self.assertIsNotNone(invoice.confirmed_at)
        log = IntegrationLog.objects.get(action="AGT_SEND", invoice=invoice)
        self.assertTrue(log.simulated)

    def test_real_environment_fails_explicitly_without_inventing(self):
        AgtConfiguration.objects.filter(pk=self.config.pk).update(environment="PRODUCTION")
        invoice = self.new()
        enqueue(invoice)
        process_queue()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.ERROR)
        self.assertIn("especificação técnica oficial", invoice.last_error)
        self.assertFalse(invoice.simulated)

    def test_retry_with_backoff_then_error(self):
        AgtConfiguration.objects.filter(pk=self.config.pk).update(max_attempts=3, retry_delay_seconds=60)
        invoice = self.new()
        enqueue(invoice)
        now = timezone.now()
        with self.fake_client(AgtResult(Outcome.RETRY, "timeout"), AgtResult(Outcome.RETRY, "503"),
                              AgtResult(Outcome.RETRY, "timeout")):
            process_queue(now=now)
            invoice.refresh_from_db()
            self.assertEqual((invoice.status, invoice.send_attempts), (InvoiceStatus.QUEUED, 1))
            self.assertEqual(invoice.next_attempt_at, now + timedelta(seconds=60))

            self.assertEqual(process_queue(now=now).processed, 0)  # ainda não chegou a hora

            process_queue(now=now + timedelta(seconds=61))
            invoice.refresh_from_db()
            self.assertEqual(invoice.send_attempts, 2)
            self.assertEqual(invoice.next_attempt_at, now + timedelta(seconds=61 + 120))

            process_queue(now=now + timedelta(days=1))
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.send_attempts), (InvoiceStatus.ERROR, 3))
        self.assertIn("Tentativas esgotadas", invoice.last_error)

    def test_backoff_capped_at_24h(self):
        AgtConfiguration.objects.filter(pk=self.config.pk).update(max_attempts=20, retry_delay_seconds=86400)
        invoice = self.new()
        enqueue(invoice)
        Invoice.objects.filter(pk=invoice.pk).update(send_attempts=10)
        now = timezone.now()
        with self.fake_client(AgtResult(Outcome.RETRY, "x")):
            process_queue(now=now)
        invoice.refresh_from_db()
        self.assertEqual(invoice.next_attempt_at, now + timedelta(hours=24))

    def test_rejected_and_pending(self):
        a, b = self.new(1), self.new(2)
        enqueue(a)
        enqueue(b)
        with self.fake_client(AgtResult(Outcome.REJECTED, "NIF inválido", request_id="R1", http_status=422),
                              AgtResult(Outcome.PENDING, "recebido", request_id="R2")):
            stats = process_queue()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((stats.rejected, stats.sent), (1, 1))
        self.assertEqual((a.status, a.agt_message, a.agt_request_id), (InvoiceStatus.REJECTED, "NIF inválido", "R1"))
        self.assertEqual((b.status, b.agt_request_id), (InvoiceStatus.SENT, "R2"))
        self.assertEqual(IntegrationLog.objects.get(invoice=a, action="AGT_SEND").response_code, 422)

    def test_unexpected_exception_is_retry(self):
        invoice = self.new()
        enqueue(invoice)
        with self.fake_client(ConnectionError("rede em baixo")):
            process_queue()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.QUEUED)
        self.assertIn("rede em baixo", invoice.last_error)

    def test_force_resend_rejected(self):
        invoice = self.new()
        enqueue(invoice)
        with self.fake_client(AgtResult(Outcome.REJECTED, "x")):
            process_queue()
        invoice.refresh_from_db()
        with self.assertRaises(QueueError):
            enqueue(invoice)  # sem force não
        self.assertTrue(enqueue(invoice, force=True))
        with self.assertRaises(QueueError):
            enqueue(self.new(5), force=True)  # force só serve para REJECTED/ERROR
        self.assertEqual((invoice.status, invoice.send_attempts), (InvoiceStatus.QUEUED, 0))

    def test_claim_prevents_double_send(self):
        invoice = self.new()
        enqueue(invoice)
        Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.SENDING)  # outro processo reservou
        self.assertEqual(process_queue().processed, 0)

    def test_stuck_sending_needs_operator(self):
        invoice = self.new()
        old = timezone.now() - timedelta(hours=1)
        Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.SENDING, updated_at=old)
        self.assertEqual(list(stuck_sending(self.company)), [invoice])
        self.assertEqual(process_queue().processed, 0)  # nunca reenviado automaticamente
        self.assertTrue(requeue_stuck(invoice))
        self.assertFalse(requeue_stuck(invoice))
        self.assertEqual(process_queue().confirmed, 1)

    def test_company_filter_limit_and_inactive(self):
        other = Company.objects.create(name="Outro", nif="5000000001")
        other_source = DataSource.objects.create(company=other, code="API", name="API", kind="API")
        for n in (1, 2, 3):
            enqueue(self.new(n))
        enqueue(import_document(other_source, numbered(9)).invoice)
        self.assertEqual(process_queue(company=other).processed, 1)
        self.assertEqual(process_queue(limit=2).processed, 2)
        Company.objects.filter(pk=self.company.pk).update(active=False)
        self.assertEqual(process_queue().processed, 0)

    def test_payload_has_lines_and_no_secrets(self):
        payload = build_payload(self.new())
        self.assertEqual(payload["company_nif"], "5000000000")
        self.assertEqual(len(payload["lines"]), 2)
        self.assertEqual(payload["total"], "164.00")

    def test_command(self):
        enqueue(self.new())
        out = StringIO()
        call_command("process_agt_queue", stdout=out)
        self.assertIn("1 processados: 1 validados", out.getvalue())
        Invoice.objects.update(status=InvoiceStatus.SENDING, updated_at=timezone.now() - timedelta(hours=2))
        out = StringIO()
        call_command("process_agt_queue", "--company", "5000000000", stdout=out)
        self.assertIn("presos", out.getvalue())
