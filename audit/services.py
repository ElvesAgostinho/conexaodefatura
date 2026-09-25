"""Gravação de logs com mascaramento obrigatório de dados sensíveis."""

from __future__ import annotations

import json
import re
from typing import Any

from config.logging_utils import MASK, mask_secrets

from .models import AuditEvent, IntegrationLog

MAX_BODY_LENGTH = 20_000

# Chaves de dicionário cujo valor nunca é gravado.
_SENSITIVE_KEY = re.compile(
    r"(?i)(pass(word|wd)?|pwd|secret|token|api[_-]?key|private[_-]?key|authorization|credential|signature|cookie)"
)


def sanitize(value: Any) -> Any:
    """Mascara recursivamente valores de chaves sensíveis e segredos em texto."""
    if isinstance(value, dict):
        return {
            key: MASK if _SENSITIVE_KEY.search(str(key)) and value[key] not in (None, "") else sanitize(value[key])
            for key in value
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return mask_secrets(value)
    return value


def _to_text(body: Any) -> str:
    if body is None or body == "":
        return ""
    if isinstance(body, (dict, list, tuple)):
        text = json.dumps(sanitize(body), ensure_ascii=False, default=str)
    else:
        text = mask_secrets(str(body))
    if len(text) > MAX_BODY_LENGTH:
        text = text[:MAX_BODY_LENGTH] + f"... [truncado, {len(text)} caracteres]"
    return text


def log_integration(
    action: str,
    *,
    company=None,
    invoice=None,
    request_id: str = "",
    status: str = "",
    response_code: int | None = None,
    response_body: Any = None,
    error_message: str = "",
    simulated: bool = False,
) -> IntegrationLog:
    if invoice is not None and company is None:
        company = invoice.company
    return IntegrationLog.objects.create(
        company=company,
        invoice=invoice,
        action=action,
        request_id=request_id or "",
        status=status or "",
        response_code=response_code,
        response_body=_to_text(response_body),
        error_message=_to_text(error_message),
        simulated=simulated,
    )


def client_ip(request) -> str | None:
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None


def audit_event(
    action: str,
    *,
    request=None,
    user=None,
    actor: str = "",
    company=None,
    obj=None,
    details: dict | None = None,
) -> AuditEvent:
    if request is not None and user is None:
        candidate = getattr(request, "user", None)
        user = candidate if getattr(candidate, "is_authenticated", False) and hasattr(candidate, "pk") else None
    if not actor:
        actor = user.get_username() if user is not None else "sistema"
    return AuditEvent.objects.create(
        company=company,
        user=user,
        actor=actor[:150],
        action=action,
        object_type=type(obj).__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and getattr(obj, "pk", None) is not None else "",
        details=sanitize(details or {}),
        ip_address=client_ip(request),
    )
