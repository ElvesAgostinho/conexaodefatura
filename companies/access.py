"""Regras de acesso por empresa. Todas as consultas de documentos passam por aqui."""

from django.db.models import QuerySet

from .models import Company, Membership

Role = Membership.Role

# Papel mínimo para cada tipo de ação.
_ROLE_LEVEL = {Role.VIEWER: 1, Role.OPERATOR: 2, Role.ADMIN: 3}


def companies_for_user(user) -> QuerySet[Company]:
    if not getattr(user, "is_authenticated", False):
        return Company.objects.none()
    if user.is_superuser:
        return Company.objects.filter(active=True)
    return Company.objects.filter(active=True, memberships__user=user).distinct()


def role_for(user, company: Company) -> str | None:
    if not getattr(user, "is_authenticated", False) or company is None or not company.active:
        return None
    if user.is_superuser:
        return Role.ADMIN
    membership = Membership.objects.filter(user=user, company=company).only("role").first()
    return membership.role if membership else None


def has_role(user, company: Company, minimum: str) -> bool:
    role = role_for(user, company)
    return role is not None and _ROLE_LEVEL[role] >= _ROLE_LEVEL[minimum]
