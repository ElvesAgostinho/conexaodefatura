"""Ligações (origens de documentos): base de dados ou API, configuradas no painel."""

import json

from django.contrib import messages
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from audit.services import audit_event
from companies.access import Role
from companies.models import ApiKey
from host_connector.catalog import CatalogError, list_tables, resolve_table, sample_rows, table_columns
from host_connector.config import HostConfigError
from invoices.canonical import HEADER_FIELDS
from invoices.testing import VALID_DOCUMENT
from sources.assistant import AssistantChoices, build_mapping, preview
from sources.mapping import validate_mapping
from sources.models import DataSource
from sources.sync import SyncError, database_for, sync_source

from .access import can, company_view, safe_next
from .forms import (ApiKeyForm, ApiSourceForm, DatabaseSourceForm, MappingAssistantForm, MappingJSONForm,
                    TablesForm)


def _source(request, pk, kind=None):
    filters = {"pk": pk, "company": request.company}
    if kind:
        filters["kind"] = kind
    return get_object_or_404(DataSource, **filters)


def _open_db(request, source):
    """Abre a ligação; em caso de configuração inválida mostra a mensagem e devolve None."""
    try:
        return database_for(source)
    except HostConfigError as exc:
        messages.error(request, f"Ligação por configurar: {exc}")
        return None


# ------------------------------------------------------------------ lista


@company_view()
def connection_list(request):
    sources = request.company.sources.annotate(n_invoices=Count("invoices")).order_by("kind", "code")
    return render(request, "dashboard/connections/list.html", {
        "sources": sources,
        "can_operate": can(request, Role.OPERATOR),
        "can_admin": can(request, Role.ADMIN),
    })


# ------------------------------------------------------------ criar/editar


@company_view(Role.ADMIN)
def connection_form(request, kind=None, pk=None):
    instance = _source(request, pk) if pk else None
    kinds = {"base-de-dados": DataSource.Kind.DATABASE, "api": DataSource.Kind.API}
    if instance is None and kind not in kinds:
        raise Http404
    kind = instance.kind if instance else kinds[kind]
    form_class = DatabaseSourceForm if kind == DataSource.Kind.DATABASE else ApiSourceForm
    action = request.POST.get("action", "save")
    extra = {"keep_secrets": action == "test"} if form_class is DatabaseSourceForm else {}
    form = form_class(request.POST or None, instance=instance, company=request.company, **extra)
    test_result = None

    if request.method == "POST":
        valid = form.is_valid()
        if action == "test" and form_class is DatabaseSourceForm:
            # Testa com os valores do formulário, sem guardar.
            test_result = _test(form.instance) if valid or not _connection_errors(form) else None
            if test_result is None:
                messages.error(request, "Corrija os campos da ligação antes de testar.")
        elif valid:
            source = form.save()
            audit_event("SOURCE_UPDATED" if instance else "SOURCE_CREATED", request=request,
                        company=request.company, obj=source,
                        details={"code": source.code, "kind": source.kind,
                                 "changed": [f for f in form.changed_data if f not in ("db_password", "db_url")]
                                 + (["senha"] if form.cleaned_data.get("db_password") else [])})
            messages.success(request, f"Ligação {source.code} guardada.")
            return redirect("dashboard:connection", pk=source.pk)

    return render(request, "dashboard/connections/form.html", {
        "form": form, "source": instance, "kind": kind, "test_result": test_result,
        "is_database": kind == DataSource.Kind.DATABASE,
    })


def _connection_errors(form) -> bool:
    connection_fields = {"connection_mode", "db_engine", "db_host", "db_port", "db_name", "db_user",
                         "db_options", "connection", "db_password", "db_url"}
    return bool(set(form.errors) & connection_fields)


def _test(source):
    try:
        db = source.connection_config()
    except HostConfigError as exc:
        return {"ok": False, "message": str(exc)}
    from host_connector.connection import HostDatabase

    database = HostDatabase(db)
    try:
        result = database.test_connection()
    finally:
        database.dispose()
    return {"ok": result.ok, "message": result.message, "elapsed_ms": result.elapsed_ms,
            "server_version": result.server_version, "measures": result.read_only_measures}


# -------------------------------------------------------------- detalhe


def _example_document() -> dict:
    """Exemplo para a documentação da API (valores fictícios)."""
    example = json.loads(json.dumps(VALID_DOCUMENT))
    example["lines"][1]["tax_exemption_code"] = "<código de isenção>"
    return example


@company_view()
def connection_detail(request, pk):
    source = _source(request, pk)
    context = {
        "source": source,
        "can_operate": can(request, Role.OPERATOR),
        "can_admin": can(request, Role.ADMIN),
        "recent": source.invoices.order_by("-id")[:8],
        "n_invoices": source.invoices.count(),
    }
    if source.kind == DataSource.Kind.API:
        endpoint = request.build_absolute_uri(reverse("api:documents"))
        context.update({
            "endpoint": endpoint,
            "ping": request.build_absolute_uri(reverse("api:ping")),
            "keys": source.api_keys.order_by("-created_at"),
            "key_form": ApiKeyForm(company=request.company, source=source),
            "example_json": json.dumps(_example_document(), indent=2, ensure_ascii=False),
            "new_key": request.session.pop("new_api_key", None) if can(request, Role.ADMIN) else None,
        })
    else:
        context.update({
            "mapping_problems": validate_mapping(source.mapping) if source.mapping else [],
            "mapped_fields": len(source.mapping.get("fields", {})) if source.mapping else 0,
            "total_fields": len(HEADER_FIELDS),
        })
    return render(request, "dashboard/connections/detail.html", context)


@require_POST
@company_view(Role.ADMIN)
def connection_create_key(request, pk):
    source = _source(request, pk, DataSource.Kind.API)
    form = ApiKeyForm(request.POST, company=request.company, source=source)
    if form.is_valid() and form.cleaned_data["source"] == source:
        api_key, raw = ApiKey.generate(request.company, form.cleaned_data["name"], source)
        audit_event("API_KEY_CREATED", request=request, company=request.company, obj=api_key,
                    details={"name": api_key.name, "source": source.code, "prefix": api_key.prefix})
        # Mostrada uma única vez, no próximo carregamento da página (padrão POST-redirect-GET).
        request.session["new_api_key"] = raw
    else:
        messages.error(request, "Indique um nome para a chave.")
    return redirect("dashboard:connection", pk=pk)


# ----------------------------------------------------------- operações BD


@require_POST
@company_view(Role.OPERATOR)
def connection_test(request, pk):
    source = _source(request, pk, DataSource.Kind.DATABASE)
    result = _test(source)
    if result["ok"]:
        messages.success(request, f"Ligação OK ({result['elapsed_ms']} ms, servidor {result['server_version'] or '?'}).")
    else:
        messages.error(request, f"Falha na ligação: {result['message']}")
    return redirect(safe_next(request, reverse("dashboard:connection", args=[pk])))


@require_POST
@company_view(Role.OPERATOR)
def connection_sync(request, pk):
    source = _source(request, pk)
    try:
        result = sync_source(source)
    except SyncError as exc:
        messages.error(request, f"{source.code}: {exc}")
    else:
        level = messages.success if not (result.stop_reason or result.conflicts) else messages.warning
        level(request, f"{source.code}: {result.summary()}")
    audit_event("SOURCE_SYNC", request=request, company=request.company, obj=source)
    return redirect("dashboard:connection", pk=pk)


@company_view(Role.OPERATOR)
def connection_explore(request, pk):
    source = _source(request, pk, DataSource.Kind.DATABASE)
    db = _open_db(request, source)
    if db is None:
        return redirect("dashboard:connection", pk=pk)
    context = {"source": source, "tables": [], "selected": None}
    try:
        context["tables"] = list_tables(db)
        wanted = request.GET.get("tabela")
        if wanted:
            ref = resolve_table(db, wanted, context["tables"])
            context["selected"] = ref
            context["columns"] = table_columns(db, ref)
            context["sample_columns"], context["sample_rows"] = sample_rows(db, ref)
    except CatalogError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - falha de ligação/permissão, sem segredos
        messages.error(request, f"Não foi possível ler a estrutura: {db.safe_error(exc)}")
    finally:
        db.dispose()
    query = request.GET.get("q", "").strip().lower()
    if query:
        context["tables"] = [t for t in context["tables"] if query in t.qualified.lower()]
    context["q"] = query
    return render(request, "dashboard/connections/explore.html", context)


# --------------------------------------------------------- mapeamento


@company_view(Role.ADMIN)
def connection_mapping(request, pk):
    source = _source(request, pk, DataSource.Kind.DATABASE)
    db = _open_db(request, source)
    if db is None:
        return redirect("dashboard:connection", pk=pk)
    assistant = source.mapping.get("assistant", {}) if source.mapping else {}
    try:
        tables = list_tables(db)
        tables_form = TablesForm(request.GET or None, tables=tables, initial={
            "documents_table": assistant.get("documents_table"), "lines_table": assistant.get("lines_table")})
        context = {"source": source, "tables_form": tables_form, "form": None, "preview": None}
        chosen = request.GET if request.GET.get("documents_table") else (
            {"documents_table": assistant.get("documents_table"), "lines_table": assistant.get("lines_table")}
            if assistant else None)
        if chosen and chosen.get("documents_table") and chosen.get("lines_table"):
            doc_ref = resolve_table(db, chosen["documents_table"], tables)
            line_ref = resolve_table(db, chosen["lines_table"], tables)
            doc_cols = [c["name"] for c in table_columns(db, doc_ref)]
            line_cols = [c["name"] for c in table_columns(db, line_ref)]
            same_tables = (assistant.get("documents_table") == doc_ref.qualified
                           and assistant.get("lines_table") == line_ref.qualified)
            initial = MappingAssistantForm.initial_from_mapping(source.mapping) if same_tables else {}
            form = MappingAssistantForm(request.POST or None, document_columns=doc_cols, line_columns=line_cols,
                                        initial=initial)
            context.update({"form": form, "doc_ref": doc_ref, "line_ref": line_ref})
            if request.method == "POST" and form.is_valid():
                header, header_defaults, lines, line_defaults = form.split()
                mapping = build_mapping(db, AssistantChoices(
                    documents_table=doc_ref, lines_table=line_ref, header_columns=header,
                    header_defaults=header_defaults, line_columns=lines, line_defaults=line_defaults,
                    cursor_column=form.cleaned_data["cursor_column"], cursor_type=form.cleaned_data["cursor_type"],
                    link_column=form.cleaned_data["link_column"], batch_size=form.cleaned_data["batch_size"],
                ), set(doc_cols), set(line_cols))
                if request.POST.get("action") == "save":
                    problems = validate_mapping(mapping)
                    if problems:
                        for problem in problems:
                            messages.error(request, problem)
                    else:
                        source.mapping = mapping
                        source.save(update_fields=["mapping", "updated_at"])
                        audit_event("SOURCE_MAPPING_SAVED", request=request, company=request.company, obj=source,
                                    details={"documents_table": doc_ref.qualified, "lines_table": line_ref.qualified})
                        messages.success(request, "Mapeamento guardado. Já pode sincronizar.")
                        return redirect("dashboard:connection", pk=pk)
                else:
                    context["preview"] = preview(db, mapping)
                    context["generated"] = mapping
    except CatalogError as exc:
        messages.error(request, str(exc))
        return redirect("dashboard:connection", pk=pk)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Não foi possível ler a estrutura: {db.safe_error(exc)}")
        return redirect("dashboard:connection", pk=pk)
    finally:
        db.dispose()
    return render(request, "dashboard/connections/mapping.html", context)


@company_view(Role.ADMIN)
def connection_mapping_json(request, pk):
    source = _source(request, pk, DataSource.Kind.DATABASE)
    form = MappingJSONForm(request.POST or None, instance=source)
    if request.method == "POST" and form.is_valid():
        problems = validate_mapping(form.cleaned_data["mapping"]) if form.cleaned_data["mapping"] else []
        if problems:
            for problem in problems:
                form.add_error("mapping", problem)
        else:
            form.save()
            audit_event("SOURCE_MAPPING_SAVED", request=request, company=request.company, obj=source,
                        details={"editor": "json"})
            messages.success(request, "Mapeamento guardado.")
            return redirect("dashboard:connection", pk=pk)
    return render(request, "dashboard/connections/mapping_json.html", {"source": source, "form": form})
