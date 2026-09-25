"""Formato canónico de documento: leitura (estrutura) e validação (regras de negócio).

Todas as origens (BD do HOST ou de outro sistema, API) produzem um dict neste formato;
ver docs/formato_canonico.md. Dois níveis de erro:

  parse_document()    erros ESTRUTURAIS (campo em falta, data/valor ilegível): o documento
                      não pode ser guardado -> CanonicalError.
  validate_document() erros de NEGÓCIO (totais que não batem, taxa sem isenção, ...):
                      o documento é guardado com estado ERROR e os erros ficam visíveis.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

MAX_LINES = 1000
MONEY_MAX = Decimal("9999999999999.99")  # 15 dígitos (ver invoices.models.AMOUNT)
QTY_MAX = Decimal("99999999999.9999")
CENT = Decimal("0.01")

_CODE_RE = re.compile(r"^[A-Z0-9]{1,10}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_NIF_RE = re.compile(r"^[A-Za-z0-9\-/.]{1,30}$")

HEADER_FIELDS = (
    "source_document_id", "document_type", "series", "document_number", "document_date",
    "customer_name", "customer_nif", "currency", "subtotal", "tax_amount", "total",
)
REQUIRED_HEADER_FIELDS = (
    "source_document_id", "document_type", "series", "document_number", "document_date",
    "customer_name", "subtotal", "tax_amount", "total",
)
LINE_FIELDS = (
    "line_number", "product_code", "description", "quantity", "unit_price", "discount",
    "tax_rate", "tax_amount", "tax_exemption_code", "total",
)
REQUIRED_LINE_FIELDS = ("description", "quantity", "unit_price", "tax_rate", "tax_amount", "total")

_MAX_LEN = {
    "source_document_id": 100, "series": 60, "document_number": 60, "customer_name": 200,
    "product_code": 60, "description": 500, "tax_exemption_code": 20,
}


class CanonicalError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class CanonicalLine:
    line_number: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    total: Decimal
    discount: Decimal = Decimal("0")
    product_code: str = ""
    tax_exemption_code: str = ""


@dataclass
class CanonicalDocument:
    source_document_id: str
    document_type: str
    series: str
    document_number: str
    document_date: date
    customer_name: str
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    customer_nif: str = ""
    currency: str = "AOA"
    lines: list[CanonicalLine] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Representação JSON estável (valores como texto) usada no hash e nos registos."""
        return json.loads(json.dumps(asdict(self), default=str))

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ conversores


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _decimal(value: Any, name: str, errors: list[str], *, places: int, maximum: Decimal) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        errors.append(f"{name}: valor numérico inválido ({value!r}).")
        return None
    try:
        # float -> str primeiro para não herdar o erro binário (0.1 -> 0.1000000000000000055...).
        number = Decimal(str(value).strip().replace(",", ".")) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError):
        errors.append(f"{name}: valor numérico inválido ({value!r}).")
        return None
    if not number.is_finite():
        errors.append(f"{name}: valor numérico inválido ({value!r}).")
        return None
    quantum = Decimal(1).scaleb(-places)
    if number != number.quantize(quantum, rounding=ROUND_HALF_UP):
        errors.append(f"{name}: no máximo {places} casas decimais ({value}).")
        return None
    number = number.quantize(quantum)
    if abs(number) > maximum:
        errors.append(f"{name}: valor fora do limite permitido ({value}).")
        return None
    return number


def _date(value: Any, name: str, errors: list[str]) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for parser in (date.fromisoformat, lambda t: datetime.fromisoformat(t).date()):
        try:
            return parser(text)
        except ValueError:
            continue
    errors.append(f"{name}: data inválida ({value!r}); use AAAA-MM-DD.")
    return None


def _check_len(name: str, value: str, errors: list[str]) -> None:
    limit = _MAX_LEN.get(name)
    if limit and len(value) > limit:
        errors.append(f"{name}: máximo {limit} caracteres.")


# ---------------------------------------------------------------------- leitura


def parse_document(data: Any) -> CanonicalDocument:
    """Converte e verifica a estrutura. Lança CanonicalError com todos os erros encontrados."""
    if not isinstance(data, dict):
        raise CanonicalError(["O documento tem de ser um objeto JSON."])
    errors: list[str] = []

    unknown = sorted(set(data) - set(HEADER_FIELDS) - {"lines"})
    if unknown:
        errors.append(f"Campos desconhecidos: {', '.join(map(str, unknown))}.")

    for name in REQUIRED_HEADER_FIELDS:
        if data.get(name) is None or _text(data.get(name)) == "":
            errors.append(f"{name}: obrigatório.")

    texts = {}
    for name in ("source_document_id", "document_type", "series", "document_number", "customer_name",
                 "customer_nif", "currency"):
        texts[name] = _text(data.get(name))
        _check_len(name, texts[name], errors)
    texts["document_type"] = texts["document_type"].upper()
    texts["currency"] = (texts["currency"] or "AOA").upper()
    if texts["document_type"] and not _CODE_RE.match(texts["document_type"]):
        errors.append("document_type: código inválido (letras/números, até 10).")
    if not _CURRENCY_RE.match(texts["currency"]):
        errors.append("currency: código ISO de 3 letras (ex.: AOA).")
    if texts["customer_nif"] and not _NIF_RE.match(texts["customer_nif"]):
        errors.append("customer_nif: formato inválido.")

    document_date = _date(data.get("document_date"), "document_date", errors)
    amounts = {
        name: _decimal(data.get(name), name, errors, places=2, maximum=MONEY_MAX)
        for name in ("subtotal", "tax_amount", "total")
    }

    raw_lines = data.get("lines")
    lines: list[CanonicalLine] = []
    if not isinstance(raw_lines, list) or not raw_lines:
        errors.append("lines: o documento tem de ter pelo menos uma linha.")
    elif len(raw_lines) > MAX_LINES:
        errors.append(f"lines: máximo {MAX_LINES} linhas por documento.")
    else:
        for index, raw in enumerate(raw_lines, start=1):
            line = _parse_line(raw, index, errors)
            if line is not None:
                lines.append(line)

    if errors:
        raise CanonicalError(errors)
    return CanonicalDocument(
        source_document_id=texts["source_document_id"],
        document_type=texts["document_type"],
        series=texts["series"],
        document_number=texts["document_number"],
        document_date=document_date,
        customer_name=texts["customer_name"],
        customer_nif=texts["customer_nif"],
        currency=texts["currency"],
        subtotal=amounts["subtotal"],
        tax_amount=amounts["tax_amount"],
        total=amounts["total"],
        lines=lines,
    )


def _parse_line(raw: Any, index: int, errors: list[str]) -> CanonicalLine | None:
    where = f"linha {index}"
    if not isinstance(raw, dict):
        errors.append(f"{where}: tem de ser um objeto.")
        return None
    before = len(errors)
    unknown = sorted(set(raw) - set(LINE_FIELDS))
    if unknown:
        errors.append(f"{where}: campos desconhecidos: {', '.join(map(str, unknown))}.")
    for name in REQUIRED_LINE_FIELDS:
        if raw.get(name) is None or _text(raw.get(name)) == "":
            errors.append(f"{where}: {name} obrigatório.")

    line_number = raw.get("line_number", index)
    try:
        line_number = int(_text(line_number) or index)
        if line_number < 1:
            raise ValueError
    except ValueError:
        errors.append(f"{where}: line_number tem de ser um inteiro positivo.")
        line_number = index

    texts = {name: _text(raw.get(name)) for name in ("description", "product_code", "tax_exemption_code")}
    for name, value in texts.items():
        _check_len(name, value, errors)

    def num(name, places, maximum):
        return _decimal(raw.get(name), f"{where}: {name}", errors, places=places, maximum=maximum)

    values = {
        "quantity": num("quantity", 4, QTY_MAX),
        "unit_price": num("unit_price", 4, QTY_MAX),
        "discount": num("discount", 2, MONEY_MAX) or Decimal("0.00"),
        "tax_rate": num("tax_rate", 4, Decimal("100")),
        "tax_amount": num("tax_amount", 2, MONEY_MAX),
        "total": num("total", 2, MONEY_MAX),
    }
    if len(errors) > before:
        return None
    return CanonicalLine(line_number=line_number, **texts, **values)


# -------------------------------------------------------------------- validação


def validate_document(doc: CanonicalDocument, *, today: date | None = None) -> list[str]:
    """Regras de negócio. Devolve a lista de erros (vazia = válido)."""
    errors: list[str] = []
    today = today or timezone.localdate()

    if doc.document_date > today + timedelta(days=1):
        errors.append(f"Data do documento no futuro ({doc.document_date}).")

    numbers = [line.line_number for line in doc.lines]
    if len(numbers) != len(set(numbers)):
        errors.append("Números de linha repetidos.")

    for line in doc.lines:
        where = f"Linha {line.line_number}"
        for name in ("quantity", "unit_price", "discount", "tax_rate", "tax_amount", "total"):
            if getattr(line, name) < 0:
                errors.append(f"{where}: {name} não pode ser negativo.")
        if line.quantity == 0:
            errors.append(f"{where}: quantidade zero.")
        expected_total = (line.quantity * line.unit_price - line.discount).quantize(CENT, ROUND_HALF_UP)
        if abs(expected_total - line.total) > CENT:
            errors.append(f"{where}: total {line.total} ≠ quantidade × preço − desconto ({expected_total}).")
        expected_tax = (line.total * line.tax_rate / 100).quantize(CENT, ROUND_HALF_UP)
        if abs(expected_tax - line.tax_amount) > CENT:
            errors.append(f"{where}: imposto {line.tax_amount} ≠ total × taxa ({expected_tax}).")
        if line.tax_rate == 0 and not line.tax_exemption_code:
            errors.append(f"{where}: taxa 0% exige código de isenção.")

    for name in ("subtotal", "tax_amount", "total"):
        if getattr(doc, name) < 0:
            errors.append(f"{name} não pode ser negativo.")
    lines_subtotal = sum((line.total for line in doc.lines), Decimal("0"))
    lines_tax = sum((line.tax_amount for line in doc.lines), Decimal("0"))
    if abs(lines_subtotal - doc.subtotal) > CENT:
        errors.append(f"Subtotal {doc.subtotal} ≠ soma das linhas ({lines_subtotal}).")
    # O imposto pode ser arredondado por linha ou por taxa: tolera 1 cêntimo por linha.
    if abs(lines_tax - doc.tax_amount) > CENT * max(1, len(doc.lines)):
        errors.append(f"Imposto {doc.tax_amount} ≠ soma do imposto das linhas ({lines_tax}).")
    if abs(doc.subtotal + doc.tax_amount - doc.total) > CENT:
        errors.append(f"Total {doc.total} ≠ subtotal + imposto ({doc.subtotal + doc.tax_amount}).")
    return errors
