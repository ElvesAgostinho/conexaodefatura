"""Importação e validação de documentos, comum a todas as origens (BD, API).

Regras:
  * A mesma origem + ID original nunca cria dois documentos (idempotente).
  * O mesmo documento fiscal (tipo + série + número) nunca existe duas vezes na empresa.
  * Um documento já comunicado à AGT nunca é alterado: dados diferentes -> conflito.
  * Um documento ainda não comunicado (ex.: com erros de validação) pode ser corrigido
    na origem e reimportado.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import log_integration, sanitize

from .canonical import CanonicalDocument, CanonicalError, parse_document, validate_document
from .models import Invoice, InvoiceItem, InvoiceStatus


class DocumentConflict(Exception):
    """O documento colide com outro já existente e não pode ser importado/alterado."""

    def __init__(self, message: str, invoice: Invoice | None = None):
        self.invoice = invoice
        super().__init__(message)


@dataclass
class ImportResult:
    invoice: Invoice
    created: bool = False
    updated: bool = False
    duplicate: bool = False


def import_document(source, data: dict) -> ImportResult:
    """Importa um documento no formato canónico vindo de `source` (sources.DataSource).

    Lança CanonicalError (estrutura inválida) ou DocumentConflict.
    """
    doc = parse_document(data)
    fingerprint = doc.fingerprint()
    company = source.company

    try:
        with transaction.atomic():
            existing = (
                Invoice.objects.select_for_update()
                .filter(company=company, source_system=source.code, source_document_id=doc.source_document_id)
                .first()
            )
            if existing is not None:
                if existing.content_hash == fingerprint:
                    return ImportResult(existing, duplicate=True)
                if existing.was_communicated:
                    raise DocumentConflict(
                        f"O documento {existing.document_number} já foi comunicado à AGT e a origem "
                        "enviou dados diferentes. Documentos comunicados não são alterados.",
                        existing,
                    )
                _check_fiscal_number_free(company, doc, exclude_pk=existing.pk)
                invoice = _save(existing, source, doc, data, fingerprint)
                result = ImportResult(invoice, updated=True)
            else:
                _check_fiscal_number_free(company, doc)
                invoice = _save(Invoice(company=company), source, doc, data, fingerprint)
                result = ImportResult(invoice, created=True)
    except IntegrityError as exc:
        # Corrida com outra importação simultânea do mesmo documento.
        again = Invoice.objects.filter(
            company=company, source_system=source.code, source_document_id=doc.source_document_id
        ).first()
        if again is not None and again.content_hash == fingerprint:
            return ImportResult(again, duplicate=True)
        raise DocumentConflict("O documento colide com outro já existente.") from exc

    log_integration(
        "IMPORT", invoice=result.invoice, status=result.invoice.status,
        response_body={"source": source.code, "updated": result.updated,
                       "validation_errors": result.invoice.validation_errors},
    )
    _auto_queue(result.invoice)
    return result


def _check_fiscal_number_free(company, doc: CanonicalDocument, exclude_pk=None) -> None:
    clash = Invoice.objects.filter(
        company=company, document_type=doc.document_type, series=doc.series, document_number=doc.document_number
    )
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    other = clash.only("source_system", "source_document_id").first()
    if other is not None:
        raise DocumentConflict(
            f"O documento {doc.document_type} {doc.document_number} (série {doc.series}) já existe, "
            f"importado da origem {other.source_system} (ID {other.source_document_id}).",
            other,
        )


def _save(invoice: Invoice, source, doc: CanonicalDocument, data: dict, fingerprint: str) -> Invoice:
    invoice.source = source
    invoice.source_system = source.code
    invoice.source_document_id = doc.source_document_id
    invoice.source_data = sanitize(json.loads(json.dumps(data, default=str)))
    invoice.content_hash = fingerprint
    for name in ("document_type", "series", "document_number", "document_date", "customer_name",
                 "customer_nif", "currency", "subtotal", "tax_amount", "total", "document_hash", "hash_control"):
        setattr(invoice, name, getattr(doc, name))
    errors = validate_document(doc)
    invoice.validation_errors = errors
    invoice.status = InvoiceStatus.ERROR if errors else InvoiceStatus.VALIDATED
    invoice.validated_at = None if errors else timezone.now()
    invoice.last_error = ""
    invoice.save()

    invoice.items.all().delete()
    InvoiceItem.objects.bulk_create(
        InvoiceItem(
            invoice=invoice, line_number=line.line_number, product_code=line.product_code,
            description=line.description, quantity=line.quantity, unit_price=line.unit_price,
            discount=line.discount, tax_rate=line.tax_rate, tax_amount=line.tax_amount,
            tax_exemption_code=line.tax_exemption_code, total=line.total,
        )
        for line in doc.lines
    )
    return invoice


def revalidate(invoice: Invoice) -> list[str]:
    """Volta a validar um documento ainda não comunicado a partir das linhas guardadas."""
    if invoice.was_communicated:
        raise DocumentConflict("Documento já comunicado à AGT: não é revalidado.", invoice)
    data = {name: getattr(invoice, name) for name in (
        "source_document_id", "document_type", "series", "document_number", "document_date",
        "customer_name", "customer_nif", "currency", "subtotal", "tax_amount", "total",
        "document_hash", "hash_control")}
    data["lines"] = [
        {name: getattr(item, name) for name in (
            "line_number", "product_code", "description", "quantity", "unit_price", "discount",
            "tax_rate", "tax_amount", "tax_exemption_code", "total")}
        for item in invoice.items.all()
    ]
    try:
        errors = validate_document(parse_document(data))
    except CanonicalError as exc:
        errors = exc.errors
    invoice.validation_errors = errors
    invoice.status = InvoiceStatus.ERROR if errors else InvoiceStatus.VALIDATED
    invoice.validated_at = None if errors else timezone.now()
    invoice.save(update_fields=["validation_errors", "status", "validated_at", "updated_at"])
    _auto_queue(invoice)
    return errors


def _auto_queue(invoice: Invoice) -> None:
    from agt.queue import enqueue_if_auto

    enqueue_if_auto(invoice)
