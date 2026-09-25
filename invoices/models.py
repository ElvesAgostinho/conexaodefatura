"""Modelo interno normalizado de fatura (independente da estrutura do HOST).

Convenções de valores (ver docs/formato_canonico.md):
  InvoiceItem.total      = quantity * unit_price - discount   (sem imposto)
  InvoiceItem.tax_amount = imposto da linha
  Invoice.subtotal       = soma de InvoiceItem.total
  Invoice.tax_amount     = soma de InvoiceItem.tax_amount
  Invoice.total          = subtotal + tax_amount
"""

from django.db import models

from companies.models import Company


class InvoiceStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    IMPORTED = "IMPORTED", "Importada"
    VALIDATED = "VALIDATED", "Validada"
    QUEUED = "QUEUED", "Em fila"
    SENDING = "SENDING", "A enviar"
    SENT = "SENT", "Enviada"
    CONFIRMED = "CONFIRMED", "Validada pela AGT"
    REJECTED = "REJECTED", "Rejeitada"
    ERROR = "ERROR", "Erro"


# Agrupamentos usados no menu do painel.
STATUS_GROUPS = {
    "pendentes": (InvoiceStatus.PENDING, InvoiceStatus.IMPORTED, InvoiceStatus.VALIDATED, InvoiceStatus.QUEUED,
                  InvoiceStatus.SENDING),
    "enviadas": (InvoiceStatus.SENT, InvoiceStatus.CONFIRMED),
    "rejeitadas": (InvoiceStatus.REJECTED,),
    "erros": (InvoiceStatus.ERROR,),
}

# 15 dígitos significativos: exatos em todos os SGBD suportados (o SQLite guarda decimais
# como vírgula flutuante de 64 bits, que só garante 15). Até 9 999 999 999 999,99.
AMOUNT = {"max_digits": 15, "decimal_places": 2}


class Invoice(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="invoices")

    # Origem (identificação única para impedir duplicados). source_system = código da origem.
    source = models.ForeignKey("sources.DataSource", on_delete=models.PROTECT, null=True, blank=True,
                               related_name="invoices", verbose_name="origem")
    source_system = models.CharField("sistema de origem", max_length=30)
    source_document_id = models.CharField("ID original", max_length=100)
    source_data = models.JSONField("dados recebidos", default=dict, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, editable=False)

    # Documento
    document_type = models.CharField("tipo de documento", max_length=10)
    document_number = models.CharField("número", max_length=60)
    series = models.CharField("série", max_length=60)
    document_date = models.DateField("data")
    customer_name = models.CharField("cliente", max_length=200)
    customer_nif = models.CharField("NIF do cliente", max_length=30, blank=True)
    document_hash = models.TextField("hash / assinatura da fatura", blank=True,
                                     help_text="Valor original do sistema de faturação (o Gateway não assina).")
    hash_control = models.CharField("controlo do hash (versão da chave)", max_length=70, blank=True)
    currency = models.CharField("moeda", max_length=3, default="AOA")
    subtotal = models.DecimalField("subtotal", **AMOUNT)
    tax_amount = models.DecimalField("imposto", **AMOUNT)
    total = models.DecimalField("total", **AMOUNT)

    # Estado no Gateway
    status = models.CharField("estado", max_length=10, choices=InvoiceStatus.choices,
                              default=InvoiceStatus.PENDING, db_index=True)
    validation_errors = models.JSONField("erros de validação", default=list, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)

    # Comunicação AGT
    agt_request_id = models.CharField("request ID AGT", max_length=100, blank=True)
    agt_response = models.JSONField("resposta AGT", default=dict, blank=True)
    agt_message = models.TextField("mensagem AGT", blank=True)
    simulated = models.BooleanField("simulação", default=False)
    send_attempts = models.PositiveIntegerField("tentativas de envio", default=0)
    queued_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField("próxima tentativa", null=True, blank=True, db_index=True)
    network_wait_since = models.DateTimeField("à espera de ligação à AGT desde", null=True, blank=True)
    last_error = models.TextField("último erro", blank=True)
    sent_at = models.DateTimeField("data de envio", null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "fatura"
        verbose_name_plural = "faturas"
        ordering = ["-document_date", "-id"]
        constraints = [
            # A mesma origem nunca é importada duas vezes.
            models.UniqueConstraint(fields=["company", "source_system", "source_document_id"],
                                    name="uniq_invoice_source"),
            # O mesmo documento fiscal nunca existe duas vezes, venha de onde vier.
            models.UniqueConstraint(fields=["company", "document_type", "series", "document_number"],
                                    name="uniq_invoice_fiscal_number"),
        ]
        indexes = [models.Index(fields=["company", "status"])]

    def __str__(self):
        return self.document_number

    @property
    def was_communicated(self) -> bool:
        """Já saiu (ou pode ter saído) para a AGT: os dados deixam de poder ser substituídos."""
        return bool(self.agt_request_id) or self.send_attempts > 0 or self.status in (
            InvoiceStatus.SENDING, InvoiceStatus.SENT, InvoiceStatus.CONFIRMED, InvoiceStatus.REJECTED
        )


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    line_number = models.PositiveIntegerField("linha")
    product_code = models.CharField("código", max_length=60, blank=True)
    description = models.CharField("descrição", max_length=500)
    quantity = models.DecimalField("quantidade", max_digits=15, decimal_places=4)
    unit_price = models.DecimalField("preço unitário", max_digits=15, decimal_places=4)
    discount = models.DecimalField("desconto", default=0, **AMOUNT)
    tax_rate = models.DecimalField("taxa de imposto (%)", max_digits=7, decimal_places=4)
    tax_amount = models.DecimalField("imposto", **AMOUNT)
    tax_exemption_code = models.CharField("código de isenção", max_length=20, blank=True)
    total = models.DecimalField("total (sem imposto)", **AMOUNT)

    class Meta:
        verbose_name = "linha de fatura"
        verbose_name_plural = "linhas de fatura"
        ordering = ["line_number"]
        constraints = [models.UniqueConstraint(fields=["invoice", "line_number"], name="uniq_invoice_line")]

    def __str__(self):
        return f"{self.invoice} #{self.line_number}"
