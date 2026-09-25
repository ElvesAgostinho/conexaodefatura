"""Auxiliares de testes: documentos canónicos válidos (dados fictícios)."""

from copy import deepcopy

VALID_DOCUMENT = {
    "source_document_id": "1001",
    "document_type": "FT",
    "series": "A2026",
    "document_number": "FT A2026/1",
    "document_date": "2026-01-15",
    "customer_name": "Cliente Teste, Lda",
    "customer_nif": "5000000009",
    "currency": "AOA",
    "subtotal": "150.00",
    "tax_amount": "14.00",
    "total": "164.00",
    "lines": [
        {"line_number": 1, "product_code": "ALOJ", "description": "Alojamento", "quantity": "2",
         "unit_price": "50.00", "discount": "0", "tax_rate": "14", "tax_amount": "14.00", "total": "100.00"},
        {"line_number": 2, "product_code": "TAXA", "description": "Taxa isenta", "quantity": "1",
         "unit_price": "50", "tax_rate": "0", "tax_amount": "0", "tax_exemption_code": "M00", "total": "50.00"},
    ],
}


def document(**overrides):
    data = deepcopy(VALID_DOCUMENT)
    data.update(overrides)
    return data


def numbered(n: int, **overrides):
    """Documento válido com ID/número diferentes."""
    return document(**{"source_document_id": str(n), "document_number": f"FT A2026/{n}", **overrides})
