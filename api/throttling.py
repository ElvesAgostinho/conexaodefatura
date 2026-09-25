from rest_framework.throttling import SimpleRateThrottle


class ApiKeyRateThrottle(SimpleRateThrottle):
    """Limite de pedidos por chave de API (API_RATE_LIMIT no .env, ex.: 120/min)."""

    scope = "api_key"

    def get_cache_key(self, request, view):
        if request.auth is None or not hasattr(request.auth, "prefix"):
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.auth.prefix}
