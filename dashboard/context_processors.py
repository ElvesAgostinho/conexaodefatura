from django.db.models import Count

from agt.models import AgtConfiguration
from companies.access import Role, companies_for_user, has_role
from companies.models import Membership
from invoices.models import STATUS_GROUPS, Invoice


def navigation(request):
    """Empresas acessíveis, papel, ambiente AGT e contadores para o menu do painel."""
    company = getattr(request, "company", None)
    user = getattr(request, "user", None)
    if company is None or user is None or not user.is_authenticated:
        return {}
    by_status = dict(Invoice.objects.filter(company=company).values_list("status").annotate(n=Count("id")))
    counts = {group: sum(by_status.get(s, 0) for s in statuses) for group, statuses in STATUS_GROUPS.items()}
    role = getattr(request, "role", None)
    return {
        "nav_companies": companies_for_user(user),
        "nav_company": company,
        "nav_role": role,
        "nav_role_label": Membership.Role(role).label if role else "",
        "nav_is_admin": has_role(user, company, Role.ADMIN),
        "nav_counts": counts,
        "nav_agt": AgtConfiguration.objects.filter(company=company).first(),
    }
