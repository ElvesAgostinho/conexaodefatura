"""Clientes de comunicação com a AGT.

IMPORTANTE: a integração real (endpoints, formato do pedido, autenticação, assinatura)
só será implementada a partir da especificação técnica OFICIAL da AGT. Até lá, apenas o
modo simulação envia; os ambientes TEST/PRODUCTION falham de forma explícita em vez de
comunicar com endereços ou formatos inventados.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from invoices.models import Invoice

from .models import AgtConfiguration


class Outcome:
    ACCEPTED = "ACCEPTED"  # validado pela AGT
    PENDING = "PENDING"    # recebido, validação posterior
    REJECTED = "REJECTED"  # recusado (erro de negócio): não repetir
    RETRY = "RETRY"        # falha temporária (rede, timeout, 5xx): repetir
    FATAL = "FATAL"        # falha que não se resolve a repetir (configuração)


@dataclass
class AgtResult:
    outcome: str
    message: str = ""
    request_id: str = ""
    http_status: int | None = None
    response: dict[str, Any] = field(default_factory=dict)


class AgtClient:
    simulated = False

    def __init__(self, config: AgtConfiguration):
        self.config = config

    def send(self, invoice: Invoice) -> AgtResult:
        raise NotImplementedError


class SimulatedAgtClient(AgtClient):
    """Não comunica com a AGT. Aceita todos os documentos validados pelo Gateway."""

    simulated = True

    def send(self, invoice: Invoice) -> AgtResult:
        request_id = f"SIM-{uuid.uuid4().hex[:16].upper()}"
        return AgtResult(
            outcome=Outcome.ACCEPTED,
            message="Simulação: documento aceite (nada foi enviado à AGT).",
            request_id=request_id,
            response={"simulated": True, "request_id": request_id, "document": invoice.document_number},
        )


class OfficialAgtClient(AgtClient):
    def send(self, invoice: Invoice) -> AgtResult:
        return AgtResult(
            outcome=Outcome.FATAL,
            message=(
                "A comunicação real com a AGT ainda não está implementada: aguarda a especificação "
                "técnica oficial. Use o ambiente Simulação."
            ),
        )


def client_for(config: AgtConfiguration) -> AgtClient:
    if config.is_simulation:
        return SimulatedAgtClient(config)
    return OfficialAgtClient(config)


def build_payload(invoice: Invoice) -> dict[str, Any]:
    """Documento no formato canónico do Gateway (a conversão para o formato AGT depende
    da especificação oficial e será feita no OfficialAgtClient)."""
    return {
        "company_nif": invoice.company.nif,
        "document_type": invoice.document_type,
        "series": invoice.series,
        "document_number": invoice.document_number,
        "document_date": invoice.document_date.isoformat(),
        "customer_name": invoice.customer_name,
        "customer_nif": invoice.customer_nif,
        "currency": invoice.currency,
        "subtotal": str(invoice.subtotal),
        "tax_amount": str(invoice.tax_amount),
        "total": str(invoice.total),
        "lines": [
            {
                "line_number": item.line_number, "product_code": item.product_code,
                "description": item.description, "quantity": str(item.quantity),
                "unit_price": str(item.unit_price), "discount": str(item.discount),
                "tax_rate": str(item.tax_rate), "tax_amount": str(item.tax_amount),
                "tax_exemption_code": item.tax_exemption_code, "total": str(item.total),
            }
            for item in invoice.items.all()
        ],
    }
