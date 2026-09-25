"""Painel web do Gateway. Todas as consultas são filtradas pela empresa ativa."""

from datetime import date

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from agt.models import AgtConfiguration
from agt.queue import QueueError, enqueue, process_queue, requeue_stuck, stuck_sending
from audit.models import AuditEvent, IntegrationLog
from audit.services import audit_event
from companies.access import Role, companies_for_user
from companies.models import ApiKey
from invoices.models import STATUS_GROUPS, Invoice, InvoiceStatus
from invoices.services import DocumentConflict, revalidate
from sources.models import DataSource
from sources.sync import SyncError, database_for, sync_source

from .access import SESSION_KEY, can, company_view
from .forms import AgtConfigurationForm, ApiKeyForm, DataSourceForm

GROUP_LABELS = {"pendentes": "Pendentes", "enviadas": "Enviadas", "rejeitadas": "Rejeitadas", "erros": "Erros"}
PAGE_SIZE = 50


def _page(request, queryset):
    return Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))


# ------------------------------------------------------------------ empresa


@require_POST
@company_view()
def select_company(request):
    company = companies_for_user(request.user).filter(pk=request.POST.get("company")).first()
    if company is not None:
        request.session[SESSION_KEY] = company.pk
    next_url = request.POST.get("next") or ""
    return redirect(next_url if next_url.startswith("/") and not next_url.startswith("//") else "dashboard:home")


# ------------------------------------------------------------------- início


@company_view()
def home(request):
    company = request.company
    invoices = Invoice.objects.filter(company=company)
    counts = dict(invoices.values_list("status").annotate(n=Count("id")))
    groups = [
        {"key": key, "label": GROUP_LABELS[key], "count": sum(counts.get(s, 0) for s in statuses)}
        for key, statuses in STATUS_GROUPS.items()
    ]
    config = AgtConfiguration.for_company(company)
    return render(request, "dashboard/home.html", {
        "groups": groups,
        "total": invoices.count(),
        "confirmed_amount": invoices.filter(status=InvoiceStatus.CONFIRMED).aggregate(s=Sum("total"))["s"],
        "recent": invoices.select_related("source")[:10],
        "sources": company.sources.all(),
        "agt": config,
        "agt_missing": [] if config.is_simulation else config.missing_for_real_sending(),
        "queued": counts.get(InvoiceStatus.QUEUED, 0),
        "stuck": stuck_sending(company).count(),
        "can_operate": can(request, Role.OPERATOR),
    })


@require_POST
@company_view(Role.OPERATOR)
def run_queue(request):
    stats = process_queue(company=request.company)
    audit_event("AGT_QUEUE_RUN", request=request, company=request.company, details=stats.__dict__)
    messages.success(request, f"Fila processada: {stats.processed} documento(s), {stats.confirmed} validados, "
                              f"{stats.rejected} rejeitados, {stats.retried} a repetir, {stats.failed} com erro.")
    return redirect("dashboard:home")


# ------------------------------------------------------------------ faturas


@company_view()
def invoice_list(request):
    invoices = Invoice.objects.filter(company=request.company).select_related("source")
    group = request.GET.get("grupo", "")
    status = request.GET.get("estado", "")
    query = request.GET.get("q", "").strip()
    source = request.GET.get("origem", "")
    date_from, date_to = request.GET.get("de", ""), request.GET.get("ate", "")

    if group in STATUS_GROUPS:
        invoices = invoices.filter(status__in=STATUS_GROUPS[group])
    if status in InvoiceStatus.values:
        invoices = invoices.filter(status=status)
    if query:
        invoices = invoices.filter(Q(document_number__icontains=query) | Q(customer_name__icontains=query)
                                   | Q(customer_nif__icontains=query) | Q(source_document_id=query))
    if source:
        invoices = invoices.filter(source_system=source)
    for value, lookup in ((date_from, "document_date__gte"), (date_to, "document_date__lte")):
        try:
            if value:
                invoices = invoices.filter(**{lookup: date.fromisoformat(value)})
        except ValueError:
            messages.warning(request, f"Data inválida ignorada: {value}")

    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "dashboard/invoice_list.html", {
        "page": _page(request, invoices),
        "group": group, "status": status, "q": query, "source": source, "date_from": date_from,
        "date_to": date_to, "querystring": params.urlencode(),
        "groups": GROUP_LABELS, "group_label": GROUP_LABELS.get(group), "statuses": InvoiceStatus.choices,
        "sources": request.company.sources.all(),
    })


@company_view()
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("source"), pk=pk, company=request.company)
    operator = can(request, Role.OPERATOR)
    return render(request, "dashboard/invoice_detail.html", {
        "invoice": invoice,
        "items": invoice.items.all(),
        "logs": invoice.integration_logs.all()[:50],
        "can_send": operator and not invoice.validation_errors and invoice.status == InvoiceStatus.VALIDATED,
        "can_resend": operator and not invoice.validation_errors
        and invoice.status in (InvoiceStatus.REJECTED, InvoiceStatus.ERROR),
        "can_revalidate": operator and not invoice.was_communicated,
        "is_stuck": operator and stuck_sending(request.company).filter(pk=invoice.pk).exists(),
    })


@require_POST
@company_view(Role.OPERATOR)
def invoice_action(request, pk, action):
    invoice = get_object_or_404(Invoice, pk=pk, company=request.company)
    try:
        if action == "enviar":
            done = enqueue(invoice)
            message = "Documento colocado na fila de envio."
        elif action == "reenviar":
            done = enqueue(invoice, force=True)
            message = "Documento colocado de novo na fila de envio."
        elif action == "revalidar":
            errors = revalidate(invoice)
            done, message = True, ("Documento válido." if not errors else f"{len(errors)} erro(s) de validação.")
        elif action == "desbloquear":
            done = requeue_stuck(invoice)
            message = "Documento devolvido à fila. Confirme no portal da AGT que o envio anterior não chegou."
        else:
            return redirect("dashboard:invoice", pk=pk)
    except (QueueError, DocumentConflict) as exc:
        messages.error(request, str(exc))
        return redirect("dashboard:invoice", pk=pk)
    if done:
        audit_event(f"INVOICE_{action.upper()}", request=request, company=request.company, obj=invoice,
                    details={"document_number": invoice.document_number})
        messages.success(request, message)
    else:
        messages.warning(request, "O estado do documento mudou entretanto; nada foi feito.")
    return redirect("dashboard:invoice", pk=pk)


# ------------------------------------------------------------------ origens


@company_view()
def source_list(request):
    sources = request.company.sources.annotate(n_invoices=Count("invoices"))
    return render(request, "dashboard/source_list.html", {
        "sources": sources,
        "can_operate": can(request, Role.OPERATOR),
        "can_admin": can(request, Role.ADMIN),
    })


@company_view(Role.ADMIN)
def source_form(request, pk=None):
    instance = get_object_or_404(DataSource, pk=pk, company=request.company) if pk else None
    form = DataSourceForm(request.POST or None, instance=instance, company=request.company)
    if request.method == "POST" and form.is_valid():
        source = form.save()
        audit_event("SOURCE_UPDATED" if instance else "SOURCE_CREATED", request=request, company=request.company,
                    obj=source, details={"code": source.code, "kind": source.kind, "changed": form.changed_data})
        messages.success(request, f"Origem {source.code} guardada.")
        return redirect("dashboard:sources")
    return render(request, "dashboard/source_form.html", {"form": form, "source": instance})


@require_POST
@company_view(Role.OPERATOR)
def source_sync(request, pk):
    source = get_object_or_404(DataSource, pk=pk, company=request.company)
    try:
        result = sync_source(source)
    except SyncError as exc:
        messages.error(request, f"{source.code}: {exc}")
    else:
        level = messages.success if not (result.stop_reason or result.conflicts) else messages.warning
        level(request, f"{source.code}: {result.summary()}")
    audit_event("SOURCE_SYNC", request=request, company=request.company, obj=source)
    return redirect("dashboard:sources")


@require_POST
@company_view(Role.OPERATOR)
def source_test(request, pk):
    source = get_object_or_404(DataSource, pk=pk, company=request.company, kind=DataSource.Kind.DATABASE)
    try:
        db = database_for(source)
    except Exception as exc:  # noqa: BLE001 - configuração em falta/inválida
        messages.error(request, f"{source.code}: {exc}")
        return redirect("dashboard:sources")
    try:
        result = db.test_connection()
    finally:
        db.dispose()
    if result.ok:
        messages.success(request, f"{source.code}: ligação OK ({result.elapsed_ms} ms, servidor {result.server_version or '?'}).")
    else:
        messages.error(request, f"{source.code}: {result.message}")
    return redirect("dashboard:sources")


# ---------------------------------------------------------------------- AGT


@company_view()
def agt_config(request):
    config = AgtConfiguration.for_company(request.company)
    editable = can(request, Role.ADMIN)
    form = AgtConfigurationForm(request.POST or None, instance=config)
    if not editable:
        for field in form.fields.values():
            field.disabled = True
    if request.method == "POST":
        if not editable:
            messages.error(request, "Só administradores podem alterar a configuração AGT.")
            return redirect("dashboard:agt")
        if form.is_valid():
            config = form.save(commit=False)
            config.updated_by = request.user
            config.save()
            audit_event("AGT_CONFIG_UPDATED", request=request, company=request.company, obj=config,
                        details={"changed": form.changed_data, "environment": config.environment})
            messages.success(request, "Configuração AGT guardada.")
            return redirect("dashboard:agt")
    return render(request, "dashboard/agt_config.html", {
        "form": form, "config": config, "editable": editable,
        "secrets": config.secrets_status(), "missing": config.missing_for_real_sending(),
    })


# --------------------------------------------------------------- chaves API


@company_view(Role.ADMIN)
def api_keys(request):
    form = ApiKeyForm(request.POST or None, company=request.company)
    new_key = None
    if request.method == "POST" and form.is_valid():
        api_key, new_key = ApiKey.generate(request.company, form.cleaned_data["name"], form.cleaned_data["source"])
        audit_event("API_KEY_CREATED", request=request, company=request.company, obj=api_key,
                    details={"name": api_key.name, "source": api_key.source.code, "prefix": api_key.prefix})
        form = ApiKeyForm(company=request.company)
    return render(request, "dashboard/api_keys.html", {
        "form": form, "new_key": new_key,
        "keys": ApiKey.objects.filter(company=request.company).select_related("source").order_by("-created_at"),
        "has_api_source": request.company.sources.filter(kind=DataSource.Kind.API, active=True).exists(),
        "api_url": request.build_absolute_uri(reverse("api:documents")),
    })


@require_POST
@company_view(Role.ADMIN)
def api_key_revoke(request, pk):
    api_key = get_object_or_404(ApiKey, pk=pk, company=request.company)
    if api_key.active:
        api_key.revoke()
        audit_event("API_KEY_REVOKED", request=request, company=request.company, obj=api_key,
                    details={"name": api_key.name, "prefix": api_key.prefix})
        messages.success(request, f"Chave {api_key.name} revogada.")
    return redirect("dashboard:api_keys")


# ---------------------------------------------------------------- auditoria


@company_view(Role.ADMIN)
def audit_log(request):
    tab = request.GET.get("tab", "eventos")
    if tab == "integracao":
        queryset = IntegrationLog.objects.filter(company=request.company).select_related("invoice")
    else:
        tab = "eventos"
        queryset = AuditEvent.objects.filter(company=request.company)
    return render(request, "dashboard/audit.html", {"tab": tab, "page": _page(request, queryset)})
