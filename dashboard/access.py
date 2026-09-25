"""Empresa ativa na sessão e verificação de papel em cada vista do painel."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from companies.access import Role, companies_for_user, has_role, role_for

SESSION_KEY = "company_id"


def current_company(request):
    companies = companies_for_user(request.user)
    wanted = request.session.get(SESSION_KEY)
    company = companies.filter(pk=wanted).first() if wanted else None
    if company is None:
        company = companies.first()
        if company is not None:
            request.session[SESSION_KEY] = company.pk
    return company


def company_view(minimum=Role.VIEWER):
    """Exige sessão, uma empresa acessível e o papel mínimo. Define request.company/role."""

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            company = current_company(request)
            if company is None:
                return render(request, "dashboard/no_company.html", status=403)
            if not has_role(request.user, company, minimum):
                raise PermissionDenied("Sem permissão para esta ação nesta empresa.")
            request.company = company
            request.role = role_for(request.user, company)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def safe_next(request, default: str) -> str:
    """Só aceita destinos internos (evita redirecionamentos para outros sites)."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else default


def can(request, minimum) -> bool:
    return has_role(request.user, request.company, minimum)
