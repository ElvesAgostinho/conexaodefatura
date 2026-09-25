"""Fila de envio à AGT.

Transições:
  VALIDATED --enqueue--> QUEUED --claim--> SENDING --> CONFIRMED | SENT | REJECTED
                                               |--> QUEUED (falha temporária, com espera crescente)
                                               '--> ERROR  (tentativas esgotadas / falha de configuração)

A reserva (claim) é um UPDATE condicional (status=QUEUED -> SENDING): dois processos
nunca enviam o mesmo documento, em qualquer SGBD. Documentos que ficaram em SENDING
(processo interrompido) NÃO são reenviados automaticamente, porque podem ter chegado à
AGT: aparecem no painel para decisão de um operador.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from audit.services import log_integration
from invoices.models import Invoice, InvoiceStatus

from .client import AgtResult, Outcome, client_for
from .models import AgtConfiguration

MAX_RETRY_DELAY = timedelta(hours=24)
NETWORK_MAX_DELAY = timedelta(minutes=15)
# Erros de rede (sem ligação, DNS, timeout, ligação recusada). OSError cobre os erros de
# socket e as exceções de ligação das bibliotecas HTTP.
NETWORK_ERRORS = (OSError, TimeoutError, ConnectionError)
RESENDABLE = (InvoiceStatus.REJECTED, InvoiceStatus.ERROR)


class QueueError(Exception):
    pass


def enqueue(invoice: Invoice, *, force: bool = False) -> bool:
    """Coloca na fila um documento VALIDATED; com `force`, reenvia um REJECTED/ERROR (ação de operador)."""
    allowed = RESENDABLE if force else (InvoiceStatus.VALIDATED,)
    if invoice.validation_errors:
        raise QueueError("O documento tem erros de validação: corrija-o na origem antes de enviar.")
    if invoice.status not in allowed:
        raise QueueError(f"Não é possível enviar um documento no estado {invoice.get_status_display()}.")
    now = timezone.now()
    updated = Invoice.objects.filter(pk=invoice.pk, status=invoice.status).update(
        status=InvoiceStatus.QUEUED, queued_at=now, next_attempt_at=now, last_error="",
        **({"send_attempts": 0} if force else {}),
    )
    if updated:
        invoice.refresh_from_db()
    return bool(updated)


def enqueue_if_auto(invoice: Invoice) -> bool:
    if invoice.status != InvoiceStatus.VALIDATED:
        return False
    config = AgtConfiguration.objects.filter(company=invoice.company).first()
    if config is None or not config.auto_send:
        return False
    return enqueue(invoice)


@dataclass
class QueueStats:
    processed: int = 0
    confirmed: int = 0
    sent: int = 0
    rejected: int = 0
    retried: int = 0
    failed: int = 0
    waiting_network: int = 0
    network_down: bool = False
    network_recovered: bool = False


def process_queue(*, company=None, limit: int = 100, now=None) -> QueueStats:
    now = now or timezone.now()
    stats = QueueStats()
    candidates = Invoice.objects.filter(status=InvoiceStatus.QUEUED, company__active=True).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    )
    if company is not None:
        candidates = candidates.filter(company=company)
    for pk in list(candidates.order_by("next_attempt_at", "id").values_list("pk", flat=True)[:limit]):
        claimed = Invoice.objects.filter(pk=pk, status=InvoiceStatus.QUEUED).update(
            status=InvoiceStatus.SENDING, send_attempts=F("send_attempts") + 1, updated_at=timezone.now()
        )
        if not claimed:
            continue  # outro processo reservou-o
        invoice = Invoice.objects.select_related("company").get(pk=pk)
        outcome = _send(invoice, stats, now)
        stats.processed += 1
        if outcome == Outcome.NETWORK:
            # Sem ligação: não vale a pena esperar pelo timeout de cada uma das restantes.
            stats.network_down = True
            break
        # A AGT respondeu: a ligação voltou. As faturas à espera de rede passam para já.
        if not stats.network_recovered and not client_for(AgtConfiguration.for_company(invoice.company)).simulated:
            waiting = Invoice.objects.filter(status=InvoiceStatus.QUEUED, network_wait_since__isnull=False)
            stats.network_recovered = waiting.update(next_attempt_at=now) > 0
    return stats


def _send(invoice: Invoice, stats: QueueStats, now) -> str:
    config = AgtConfiguration.for_company(invoice.company)
    client = client_for(config)
    try:
        result = client.send(invoice)
    except NETWORK_ERRORS as exc:
        result = AgtResult(outcome=Outcome.NETWORK, message=f"Sem ligação à AGT ({type(exc).__name__}: {exc})")
    except Exception as exc:  # noqa: BLE001 - erro inesperado: temporário, mas com limite de tentativas
        result = AgtResult(outcome=Outcome.RETRY, message=f"{type(exc).__name__}: {exc}")

    invoice.simulated = client.simulated
    invoice.agt_message = result.message
    invoice.agt_response = result.response
    if result.request_id:
        invoice.agt_request_id = result.request_id

    if result.outcome != Outcome.NETWORK:
        invoice.network_wait_since = None

    if result.outcome == Outcome.NETWORK:
        # Nunca chegou à AGT: não conta como tentativa e repete sem limite.
        invoice.status = InvoiceStatus.QUEUED
        invoice.send_attempts = max(0, invoice.send_attempts - 1)
        invoice.network_wait_since = invoice.network_wait_since or now
        invoice.next_attempt_at = now + min(timedelta(seconds=config.retry_delay_seconds), NETWORK_MAX_DELAY)
        invoice.last_error = result.message
        stats.waiting_network += 1
    elif result.outcome == Outcome.ACCEPTED:
        invoice.status = InvoiceStatus.CONFIRMED
        invoice.sent_at = invoice.sent_at or now
        invoice.confirmed_at = now
        invoice.next_attempt_at = None
        invoice.last_error = ""
        stats.confirmed += 1
    elif result.outcome == Outcome.PENDING:
        invoice.status = InvoiceStatus.SENT
        invoice.sent_at = now
        invoice.next_attempt_at = None
        stats.sent += 1
    elif result.outcome == Outcome.REJECTED:
        invoice.status = InvoiceStatus.REJECTED
        invoice.sent_at = invoice.sent_at or now
        invoice.next_attempt_at = None
        stats.rejected += 1
    elif result.outcome == Outcome.RETRY and invoice.send_attempts < config.max_attempts:
        delay = timedelta(seconds=config.retry_delay_seconds * 2 ** (invoice.send_attempts - 1))
        invoice.status = InvoiceStatus.QUEUED
        invoice.next_attempt_at = now + min(delay, MAX_RETRY_DELAY)
        invoice.last_error = result.message
        stats.retried += 1
    else:
        invoice.status = InvoiceStatus.ERROR
        invoice.next_attempt_at = None
        invoice.last_error = (
            result.message if result.outcome == Outcome.FATAL
            else f"Tentativas esgotadas ({invoice.send_attempts}). Último erro: {result.message}"
        )
        stats.failed += 1
    invoice.save()

    log_integration(
        "AGT_SEND", invoice=invoice, request_id=invoice.agt_request_id, status=result.outcome,
        response_code=result.http_status, response_body=result.response,
        error_message="" if result.outcome in (Outcome.ACCEPTED, Outcome.PENDING) else result.message,
        simulated=client.simulated,
    )
    return result.outcome


def requeue_stuck(invoice: Invoice) -> bool:
    """Decisão do operador: um documento preso em SENDING volta à fila (pode duplicar na AGT
    se o envio anterior tiver chegado; confirmar primeiro no portal da AGT)."""
    now = timezone.now()
    return bool(Invoice.objects.filter(pk=invoice.pk, status=InvoiceStatus.SENDING).update(
        status=InvoiceStatus.QUEUED, next_attempt_at=now, updated_at=now,
        last_error="Reenvio manual após envio interrompido."))


def stuck_sending(company=None, older_than: timedelta = timedelta(minutes=30)):
    """Documentos presos em SENDING (processo interrompido): exigem decisão manual."""
    qs = Invoice.objects.filter(status=InvoiceStatus.SENDING, updated_at__lt=timezone.now() - older_than)
    return qs.filter(company=company) if company is not None else qs
