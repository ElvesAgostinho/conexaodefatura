"""Configuração automática: detetar a base de dados do cliente e ligar-se, com autorização.

Passos:  1 autorizar a procura  →  2 servidores encontrados  →  3 entrar no servidor
         →  4 escolher a base  →  5 aprovar a criação do utilizador só de leitura
         →  mapeamento sugerido automaticamente (assistente, com pré-visualização).

As credenciais de ADMINISTRADOR nunca são gravadas: viajam cifradas no próprio formulário
e expiram ao fim de 10 minutos. Só a senha do utilizador SÓ DE LEITURA fica guardada
(cifrada) na ligação. Cada autorização fica registada na auditoria.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from audit.services import audit_event
from companies.access import Role
from config.crypto import DecryptionError, decrypt_temporary, encrypt_temporary
from host_connector.config import HostConfigError, HostConnectionConfig
from host_connector.discovery import (ENGINE_LABELS, DetectedServer, DiscoveryError, discover_local,
                                      discover_network, local_networks, validate_network)
from host_connector.provisioning import (DEFAULT_USERNAME, PROVISIONING_ENGINES, ProvisioningError, admin_config,
                                         apply_plan,
                                         provisioning_plan, server_overview, verify_read_only)
from sources.models import DataSource

from .access import company_view

SESSION_SERVERS = "setup_servers"
READONLY_USERNAME = DEFAULT_USERNAME  # nome do utilizador só de leitura criado no servidor do cliente
ACCESS_TTL = 600
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", ".")


# ------------------------------------------------------------ utilitários


def _servers(request) -> list[DetectedServer]:
    return [DetectedServer(**data) for data in request.session.get(SESSION_SERVERS, [])]


def _seal(data: dict) -> str:
    return encrypt_temporary(json.dumps(data))


def _unseal(token: str) -> dict:
    return json.loads(decrypt_temporary(token or "", ACCESS_TTL))


def _config_from_access(access: dict, database: str = "") -> HostConnectionConfig:
    return admin_config(access["engine"], access["host"], port=access.get("port"), database=database,
                        user=access.get("user", ""), password=access.get("password", ""))


def _unique_code(company, name: str) -> str:
    base = re.sub(r"[^A-Z0-9]", "", name.upper())[:20] or "BD"
    code, n = base, 2
    while DataSource.objects.filter(company=company, code=code).exists():
        code = f"{base[:17]}-{n}"
        n += 1
    return code


def _create_source(request, access: dict, database: str, user: str, password: str) -> DataSource:
    engine = access["engine"]
    source = DataSource(
        company=request.company, code=_unique_code(request.company, database),
        name=f"{ENGINE_LABELS.get(engine, engine)} · {database}", system="Detetado automaticamente",
        kind=DataSource.Kind.DATABASE, connection_mode=DataSource.ConnectionMode.PANEL,
        db_engine=engine, db_host=access["host"], db_port=access.get("port"),
        db_name=database, db_user=user, db_trust_server_certificate=engine == "mssql",
    )
    source.set_db_password(password)
    source.full_clean()
    source.save()
    audit_event("SETUP_SOURCE_CREATED", request=request, company=request.company, obj=source,
                details={"engine": engine, "host": access["host"], "database": database, "user": user})
    return source


# -------------------------------------------------------- 1. autorizar


@company_view(Role.ADMIN)
def setup_start(request):
    networks = [str(n) for n in local_networks()]
    if request.method == "POST":
        if not request.POST.get("consent_local"):
            messages.error(request, "Para procurar, é preciso autorizar a procura neste computador.")
            return redirect("dashboard:setup")
        result_servers: list[DetectedServer] = []
        warnings = []
        local = discover_local()
        result_servers += local.servers
        scope = ["este computador"]
        if request.POST.get("consent_network"):
            chosen = request.POST.get("network", "")
            try:
                if chosen not in networks:
                    raise DiscoveryError("Rede não disponível neste computador.")
                network = validate_network(chosen)
                remote = discover_network([network], own_addresses=[])
                scope.append(f"rede {network} ({remote.scanned_hosts} endereços)")
                for server in remote.servers:
                    if not any(s.key == server.key for s in result_servers):
                        result_servers.append(server)
            except DiscoveryError as exc:
                warnings.append(str(exc))
        request.session[SESSION_SERVERS] = [asdict(s) for s in result_servers]
        audit_event("SETUP_DISCOVERY_AUTHORIZED", request=request, company=request.company,
                    details={"scope": scope, "found": len(result_servers)})
        for warning in warnings:
            messages.warning(request, warning)
        return redirect("dashboard:setup_servers")
    return render(request, "dashboard/setup/start.html", {"networks": networks})


# ------------------------------------------------- 2. servidores encontrados


@company_view(Role.ADMIN)
def setup_servers(request):
    servers = _servers(request)
    return render(request, "dashboard/setup/servers.html", {
        "servers": list(enumerate(servers)),
        "searched": SESSION_SERVERS in request.session,
        "engines": [(k, v) for k, v in ENGINE_LABELS.items() if k != "firebird"],
    })


# ------------------------------------------------------ 3. entrar no servidor


@company_view(Role.ADMIN)
def setup_connect(request):
    source = request.POST if request.method == "POST" else request.GET
    servers = _servers(request)
    try:
        index = int(source.get("s", ""))
        server = servers[index]
    except (ValueError, IndexError):
        engine = source.get("engine", "")
        host = (source.get("host") or "").strip()
        if engine not in ENGINE_LABELS or not host or not re.fullmatch(r"[A-Za-z0-9._\\-]{1,120}", host):
            messages.error(request, "Escolha um servidor da lista ou indique o sistema e o servidor.")
            return redirect("dashboard:setup_servers")
        port = source.get("port") or None
        server = DetectedServer(engine, host, port=int(port) if port and str(port).isdigit() else None,
                                instance=(source.get("instance") or "").strip(), evidence=["indicado manualmente"])
        index = None

    context = {"server": server, "index": index, "can_windows": server.engine == "mssql",
               "windows_identity": windows_identity(),
               "can_auto": server.engine in PROVISIONING_ENGINES}
    if request.method != "POST":
        return render(request, "dashboard/setup/connect.html", context)

    if not request.POST.get("consent_connect"):
        messages.error(request, "É preciso autorizar a entrada no servidor.")
        return render(request, "dashboard/setup/connect.html", context)
    auth = request.POST.get("auth", "windows")
    access = {"engine": server.engine, "host": server.server_name, "port": _connection_port(server), "auth": auth,
              "user": "", "password": ""}
    if auth != "windows" or server.engine != "mssql":
        access["user"] = (request.POST.get("user") or "").strip()
        access["password"] = request.POST.get("password") or ""
        if not access["user"]:
            messages.error(request, "Indique o utilizador de administrador.")
            return render(request, "dashboard/setup/connect.html", context)
    try:
        overview = server_overview(_config_from_access(access))
    except (ProvisioningError, HostConfigError) as exc:
        context["error"] = str(exc)
        context["hint"] = _hint(str(exc), server)
        return render(request, "dashboard/setup/connect.html", context)
    audit_event("SETUP_CONNECT_AUTHORIZED", request=request, company=request.company,
                details={"engine": server.engine, "server": server.server_name, "auth": auth,
                         "login": overview.login, "databases": len(overview.databases)})
    return render(request, "dashboard/setup/databases.html", {
        "server": server, "overview": overview, "access_token": _seal(access),
        "can_auto": server.engine in PROVISIONING_ENGINES and overview.can_create_logins
        and overview.sql_logins_allowed is not False,
    })


def windows_identity() -> str:
    """Conta com que o Gateway corre (a usada na autenticação Windows do SQL Server)."""
    import getpass
    import os

    user = getpass.getuser()
    if user.upper() in ("SYSTEM", f"{os.environ.get('COMPUTERNAME', '')}$".upper()):
        return "NT AUTHORITY\\SYSTEM"
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain else user


def _connection_port(server: DetectedServer) -> int | None:
    """SQL Server com instância ou na porta 1433: sem porta (o SQL Server resolve)."""
    if server.engine == "mssql":
        named = server.instance and server.instance.upper() != "MSSQLSERVER"
        return None if named or server.port in (None, 1433) else server.port
    return server.port


def _hint(message: str, server: DetectedServer) -> str:
    text = message.lower()
    if "login failed" in text or "password authentication failed" in text or "access denied" in text:
        return "Utilizador ou senha incorretos, ou este utilizador não tem acesso ao servidor."
    if "certificate" in text or "certificado" in text:
        return "O servidor usa um certificado próprio. Tente de novo (o assistente confia no certificado)."
    if "not found" in text or "timeout" in text or "network-related" in text or "could not connect" in text:
        extra = " O TCP/IP deste SQL Server está desligado: ative-o no SQL Server Configuration Manager." \
            if server.tcp_enabled is False and server.host not in LOCAL_HOSTS else ""
        return "O servidor não respondeu. Confirme que o serviço está a correr e a rede/firewall." + extra
    return ""


# ---------------------------------------------------------- 4. escolher a base


@require_POST
@company_view(Role.ADMIN)
def setup_database(request):
    try:
        access = _unseal(request.POST.get("access", ""))
    except (DecryptionError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("dashboard:setup_servers")
    database = request.POST.get("database", "")
    known = request.POST.getlist("known")
    if not database or database not in known:
        messages.error(request, "Escolha uma base de dados da lista.")
        return redirect("dashboard:setup_servers")
    mode = request.POST.get("mode", "auto")
    admin = _config_from_access(access)
    if mode == "existing":
        user, password = (request.POST.get("ro_user") or "").strip(), request.POST.get("ro_password") or ""
        try:
            ro = HostConnectionConfig.from_values(engine=access["engine"], host=access["host"], port=access.get("port"),
                                                  name=database, user=user, password=password,
                                                  trust_server_certificate=True)
        except HostConfigError as exc:
            messages.error(request, str(exc))
            return redirect("dashboard:setup_servers")
        check = verify_read_only(ro)
        if not check.ok:
            messages.error(request, f"Não foi possível usar esse utilizador: {check.message} "
                                    f"{', '.join(check.write_permissions)}")
            return redirect("dashboard:setup_servers")
        source = _create_source(request, access, database, user, password)
        messages.success(request, "Ligação criada com o seu utilizador só de leitura.")
        return redirect(reverse("dashboard:connection_mapping", args=[source.pk]))

    login_host = "localhost" if access["host"].split("\\")[0].lower() in LOCAL_HOSTS else "%"
    try:
        plan = provisioning_plan(admin, database, username=READONLY_USERNAME, known_databases=known,
                                 login_host=login_host)
    except ProvisioningError as exc:
        messages.error(request, str(exc))
        return redirect("dashboard:setup_servers")
    return render(request, "dashboard/setup/confirm.html", {
        "access_token": request.POST.get("access"), "database": database, "plan": plan,
        "plan_token": _seal({"database": database, "username": plan.username, "password": plan.password,
                             "login_host": plan.login_host}),
        "engine_label": ENGINE_LABELS.get(access["engine"], access["engine"]), "server_name": access["host"],
    })


# ----------------------------------------------- 5. criar utilizador (aprovado)


@require_POST
@company_view(Role.ADMIN)
def setup_provision(request):
    try:
        access = _unseal(request.POST.get("access", ""))
        sealed = _unseal(request.POST.get("plan", ""))
    except (DecryptionError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("dashboard:setup_servers")
    if not request.POST.get("consent_provision"):
        messages.error(request, "É preciso aprovar a criação do utilizador só de leitura.")
        return redirect("dashboard:setup_servers")
    admin = _config_from_access(access)
    try:
        plan = provisioning_plan(admin, sealed["database"], username=sealed["username"], password=sealed["password"],
                                 known_databases=[sealed["database"]], login_host=sealed["login_host"])
        apply_plan(admin, plan)
    except ProvisioningError as exc:
        messages.error(request, str(exc))
        return redirect("dashboard:setup_servers")
    audit_event("SETUP_READONLY_USER_CREATED", request=request, company=request.company,
                details={"engine": access["engine"], "server": access["host"], "database": plan.database,
                         "user": plan.username, "sql": plan.display()})
    ro = HostConnectionConfig.from_values(engine=access["engine"], host=access["host"], port=access.get("port"),
                                          name=plan.database, user=plan.username, password=plan.password,
                                          trust_server_certificate=True)
    check = verify_read_only(ro)
    if not check.ok:
        messages.error(request, f"O utilizador foi criado mas a verificação falhou: {check.message}")
        return redirect("dashboard:setup_servers")
    source = _create_source(request, access, plan.database, plan.username, plan.password)
    request.session.pop(SESSION_SERVERS, None)
    messages.success(request, f"Ligação criada e verificada: o utilizador {plan.username} só consegue ler. "
                              "Confirme agora o mapeamento sugerido.")
    return redirect(reverse("dashboard:connection_mapping", args=[source.pk]))
