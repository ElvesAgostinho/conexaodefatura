"""Logs de integração (comunicação com HOST/AGT) e eventos de auditoria (ações de utilizadores).

Nunca guardam senhas, chaves privadas, tokens ou segredos: tudo passa por
audit.services, que mascara os dados antes de gravar.
"""

from django.conf import settings
from django.db import models


class IntegrationLog(models.Model):
    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, null=True, blank=True,
                                related_name="integration_logs")
    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="integration_logs")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField("ação", max_length=40)
    request_id = models.CharField(max_length=100, blank=True)
    status = models.CharField("estado", max_length=30, blank=True)
    response_code = models.IntegerField("código de resposta", null=True, blank=True)
    response_body = models.TextField("resposta", blank=True)
    error_message = models.TextField("erro", blank=True)
    simulated = models.BooleanField("simulação", default=False)

    class Meta:
        verbose_name = "log de integração"
        verbose_name_plural = "logs de integração"
        ordering = ["-timestamp", "-id"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.action} {self.status}"


class AuditEvent(models.Model):
    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, null=True, blank=True,
                                related_name="audit_events")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    actor = models.CharField("autor", max_length=150, blank=True)  # utilizador, chave de API ou sistema
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField("ação", max_length=60)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=60, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "evento de auditoria"
        verbose_name_plural = "eventos de auditoria"
        ordering = ["-timestamp", "-id"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.actor} {self.action}"
