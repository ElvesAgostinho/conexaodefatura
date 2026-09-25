"""Autenticação da API por chave da empresa.

Cabeçalho aceite:  Authorization: Api-Key gw_xxxx_yyyy   (ou  X-API-Key: gw_xxxx_yyyy)
A chave identifica a empresa E a origem: um sistema só vê e envia documentos da sua origem.
"""

from rest_framework import authentication, exceptions, permissions

from companies.models import ApiKey

KEYWORD = "Api-Key"


class ApiClient:
    """Principal autenticado por chave de API (não é um utilizador Django)."""

    is_authenticated = True
    is_anonymous = False

    def __init__(self, api_key: ApiKey):
        self.api_key = api_key
        self.company = api_key.company
        self.source = api_key.source

    def __str__(self):
        return f"api:{self.api_key.name}"


class ApiKeyAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        raw = request.META.get("HTTP_X_API_KEY")
        if raw is None:
            header = authentication.get_authorization_header(request).decode("latin-1")
            scheme, _, value = header.partition(" ")
            if scheme.lower() != KEYWORD.lower():
                return None
            raw = value
        api_key = ApiKey.authenticate(raw.strip())
        if api_key is None:
            raise exceptions.AuthenticationFailed("Chave de API inválida, revogada ou inativa.")
        return ApiClient(api_key), api_key

    def authenticate_header(self, request):
        return KEYWORD


class HasApiKey(permissions.BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, ApiKey)
