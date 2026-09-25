"""Settings do Gateway Fiscal HOST → Django → AGT.

Todos os valores sensíveis ou dependentes do ambiente vêm do ficheiro .env
(ver .env.example). Nunca colocar credenciais neste ficheiro.
"""

import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from .database import gateway_database
from .env import env_bool, env_int, env_list, env_str

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env_str("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY é obrigatória quando DJANGO_DEBUG=False.")
    # Apenas para desenvolvimento local; nunca usada em produção.
    SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1"] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "companies",
    "sources",
    "host_connector",
    "invoices",
    "agt",
    "dashboard",
    "audit",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "dashboard.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Base de dados própria do Gateway (ver config/database.py).
# A BD do HOST NÃO é uma base Django: é acedida só em leitura pelo host_connector.
DATABASES = {"default": gateway_database(BASE_DIR)}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Só nos testes automáticos: hash rápido (em funcionamento real mantém-se o PBKDF2 do Django).
if sys.argv[1:2] == ["test"]:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


LANGUAGE_CODE = "pt-pt"
TIME_ZONE = env_str("DJANGO_TIME_ZONE", "Africa/Luanda")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

LOGIN_URL = "dashboard:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "dashboard:login"
# O painel fecha a sessão ao fim de 8 horas.
SESSION_COOKIE_AGE = env_int("DJANGO_SESSION_AGE", 8 * 3600)
STATIC_ROOT = BASE_DIR / "staticfiles"
# O próprio Gateway serve os ficheiros estáticos (não é preciso IIS/nginx).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Segurança HTTP. Em produção (DEBUG=False) o Gateway só funciona sobre HTTPS.
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# DJANGO_HTTPS=False só para instalações numa rede interna sem certificado (http://servidor:8000).
HTTPS = env_bool("DJANGO_HTTPS", True)

if not DEBUG and HTTPS:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = False
    if env_bool("DJANGO_BEHIND_PROXY", False):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# API para sistemas externos (autenticação por chave da empresa; só JSON).
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_THROTTLE_RATES": {"api_key": env_str("API_RATE_LIMIT", "120/min")},
    "UNAUTHENTICATED_USER": None,
}
# Tamanho máximo de um pedido (um documento com 1000 linhas cabe folgadamente).
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("API_MAX_BODY_BYTES", 2 * 1024 * 1024)

# Relatórios de diagnóstico da BD do HOST (contêm estrutura interna: não versionar).
HOST_REPORTS_DIR = Path(env_str("HOST_REPORTS_DIR", str(BASE_DIR / "reports")))


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "mask_secrets": {"()": "config.logging_utils.SecretMaskingFilter"},
    },
    "formatters": {
        "default": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "filters": ["mask_secrets"],
        },
    },
    "root": {"handlers": ["console"], "level": env_str("DJANGO_LOG_LEVEL", "INFO")},
}

# Instalação como serviço: registo também em ficheiro (rotativo, com segredos mascarados).
GATEWAY_LOG_FILE = env_str("GATEWAY_LOG_FILE")
if GATEWAY_LOG_FILE:
    Path(GATEWAY_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    LOGGING["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": GATEWAY_LOG_FILE,
        "maxBytes": 5 * 1024 * 1024,
        "backupCount": 5,
        "encoding": "utf-8",
        "formatter": "default",
        "filters": ["mask_secrets"],
    }
    LOGGING["root"]["handlers"].append("file")
