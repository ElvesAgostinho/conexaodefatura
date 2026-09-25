"""Cliente sem internet: as faturas esperam e são enviadas quando a ligação voltar."""

import socket
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from agt.client import AgtResult, Outcome, SimulatedAgtClient
from agt.models import AgtConfiguration
from agt.queue import NETWORK_MAX_DELAY, enqueue, process_queue
from audit.models import IntegrationLog
from companies.models import Company
from invoices.models import Invoice, InvoiceStatus
from invoices.services import import_document
from invoices.testing import numbered
from sources.models import DataSource


class NetworkTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Hotel", nif="5000000000")
        self.source = DataSource.objects.create(company=self.company, code="API", name="API", kind="API")
        AgtConfiguration.objects.create(company=self.company, max_attempts=3, retry_delay_seconds=300)

    def queued(self, n):
        invoice = import_document(self.source, numbered(n)).invoice
        enqueue(invoice)
        return invoice

    def agt_client(self, behaviour):
        """behaviour(invoice) devolve AgtResult ou lança exceção."""
        calls = []

        class Fake(SimulatedAgtClient):
            simulated = False

            def send(inner, invoice):
                calls.append(invoice.pk)
                return behaviour(invoice)

        patcher = mock.patch("agt.queue.client_for", lambda config: Fake(config))
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


def offline(invoice):
    raise socket.gaierror(11001, "getaddrinfo failed")


class OfflineTests(NetworkTestCase):
    def test_network_failures_never_become_error(self):
        invoice = self.queued(1)
        self.agt_client(offline)
        now = timezone.now()
        for i in range(50):
            process_queue(now=now + timedelta(hours=i))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.QUEUED)
        self.assertEqual(invoice.send_attempts, 0)  # nunca chegou à AGT: não conta
        self.assertEqual(invoice.network_wait_since, now)  # desde a primeira falha
        self.assertIn("Sem ligação à AGT", invoice.last_error)
        self.assertEqual(IntegrationLog.objects.filter(action="AGT_SEND", status="NETWORK").count(), 50)

    def test_next_attempt_is_short_and_capped(self):
        invoice = self.queued(1)
        self.agt_client(offline)
        now = timezone.now()
        process_queue(now=now)
        invoice.refresh_from_db()
        self.assertEqual(invoice.next_attempt_at, now + timedelta(seconds=300))
        AgtConfiguration.objects.filter(company=self.company).update(retry_delay_seconds=86400)
        process_queue(now=now + timedelta(days=1))
        invoice.refresh_from_db()
        self.assertEqual(invoice.next_attempt_at, now + timedelta(days=1) + NETWORK_MAX_DELAY)

    def test_batch_stops_at_first_network_failure(self):
        for n in range(1, 6):
            self.queued(n)
        calls = self.agt_client(offline)
        stats = process_queue()
        self.assertEqual(len(calls), 1)  # não espera pelo timeout das outras 4
        self.assertTrue(stats.network_down)
        self.assertEqual(Invoice.objects.filter(status=InvoiceStatus.QUEUED).count(), 5)
        self.assertEqual(Invoice.objects.filter(status=InvoiceStatus.SENDING).count(), 0)  # nenhuma presa

    def test_connection_back_sends_everything(self):
        invoices = [self.queued(n) for n in range(1, 4)]
        state = {"online": False}

        def behaviour(invoice):
            if not state["online"]:
                raise ConnectionRefusedError("recusada")
            return AgtResult(Outcome.ACCEPTED, "ok", request_id=f"R{invoice.pk}")

        self.agt_client(behaviour)
        now = timezone.now()
        process_queue(now=now)
        # Só a primeira foi tentada; as outras ainda não têm espera de rede e continuam na fila.
        state["online"] = True
        stats = process_queue(now=now + timedelta(seconds=301))
        self.assertTrue(stats.network_recovered or stats.confirmed == 3)
        process_queue(now=now + timedelta(seconds=302))
        for invoice in invoices:
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, InvoiceStatus.CONFIRMED)
            self.assertIsNone(invoice.network_wait_since)
            self.assertEqual(invoice.send_attempts, 1)  # só a tentativa que chegou à AGT conta

    def test_recovery_pulls_waiting_invoices_forward(self):
        waiting = self.queued(1)
        Invoice.objects.filter(pk=waiting.pk).update(network_wait_since=timezone.now() - timedelta(hours=3),
                                                     next_attempt_at=timezone.now() + timedelta(minutes=14))
        fresh = self.queued(2)
        Invoice.objects.filter(pk=fresh.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.agt_client(lambda inv: AgtResult(Outcome.ACCEPTED, "ok"))
        now = timezone.now()
        stats = process_queue(now=now)
        self.assertTrue(stats.network_recovered)
        waiting.refresh_from_db()
        # A fatura em espera já foi enviada nesta ronda ou ficou com a próxima tentativa para já.
        self.assertTrue(waiting.status == InvoiceStatus.CONFIRMED or waiting.next_attempt_at <= now)

    def test_unexpected_errors_still_have_a_limit(self):
        invoice = self.queued(1)
        self.agt_client(lambda inv: (_ for _ in ()).throw(ValueError("erro de programação")))
        now = timezone.now()
        for i in range(5):
            process_queue(now=now + timedelta(days=i))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.ERROR)
        self.assertIn("Tentativas esgotadas (3)", invoice.last_error)

    def test_agt_5xx_retry_is_limited_and_clears_network_wait(self):
        invoice = self.queued(1)
        Invoice.objects.filter(pk=invoice.pk).update(network_wait_since=timezone.now())
        self.agt_client(lambda inv: AgtResult(Outcome.RETRY, "503 Service Unavailable"))
        now = timezone.now()
        process_queue(now=now)
        invoice.refresh_from_db()
        self.assertIsNone(invoice.network_wait_since)
        self.assertEqual((invoice.status, invoice.send_attempts), (InvoiceStatus.QUEUED, 1))

    def test_client_can_report_network_outcome(self):
        invoice = self.queued(1)
        self.agt_client(lambda inv: AgtResult(Outcome.NETWORK, "Sem ligação"))
        process_queue()
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.send_attempts), (InvoiceStatus.QUEUED, 0))
        self.assertIsNotNone(invoice.network_wait_since)


class OfflineDashboardTests(NetworkTestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        from companies.models import Membership

        user = get_user_model().objects.create_user("u", password="x")
        Membership.objects.create(user=user, company=self.company, role="VIEWER")
        self.client.force_login(user)

    def test_banner_only_after_one_hour(self):
        invoice = self.queued(1)
        Invoice.objects.filter(pk=invoice.pk).update(network_wait_since=timezone.now() - timedelta(minutes=20))
        self.assertNotContains(self.client.get("/"), "Sem ligação à AGT")
        Invoice.objects.filter(pk=invoice.pk).update(network_wait_since=timezone.now() - timedelta(hours=2))
        page = self.client.get("/")
        self.assertContains(page, "Sem ligação à AGT desde")
        self.assertContains(page, "Nada se perde")
        detail = self.client.get(f"/faturas/{invoice.pk}/")
        self.assertContains(detail, "À espera de ligação à AGT")
