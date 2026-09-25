from companies.access import Role, companies_for_user, has_role


def navigation(request):
    """Empresas acessíveis e papel na empresa ativa, para o menu do painel."""
    company = getattr(request, "company", None)
    user = getattr(request, "user", None)
    if company is None or user is None or not user.is_authenticated:
        return {}
    return {
        "nav_companies": companies_for_user(user),
        "nav_company": company,
        "nav_role": getattr(request, "role", None),
        "nav_is_admin": has_role(user, company, Role.ADMIN),
    }
