"""
Shared settings for the Ripple Innovation Labs backend.

Environment-specific settings live in ``dev.py`` and ``prod.py`` and are selected
by the ``DJANGO_ENV`` variable — see ``__init__.py``. Anything here is common to
both; values that only differ by deployment are read from the environment / .env.
"""
from datetime import timedelta
from pathlib import Path

import environ

# Use the pure-Python PyMySQL driver for MySQL when it's installed (the easiest
# MySQL option on cPanel shared hosting). Harmless if unused.
try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:
    pass

# backend/ripple/settings/base.py  ->  BASE_DIR = backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    SECRET_KEY=(str, "dev-insecure-key-do-not-use-in-production"),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:5180", "http://localhost:5173"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    FRONTEND_URL=(str, "http://localhost:5180"),
    USD_TO_NGN_RATE=(float, 1600.0),
    PAYSTACK_FEE_PERCENT=(float, 1.5),
)
# Load .env without clobbering real environment variables (so values set in the
# cPanel Python App UI take precedence over the file).
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("SECRET_KEY")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "corsheaders",
    # local
    "accounts",
    "projects",
    "payments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves Django's own static files (admin, DRF) in production.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ripple.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ripple.wsgi.application"

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# collectstatic writes here; WhiteNoise serves it in production.
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework + JWT
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
}

# CORS / CSRF — values differ per deployment via .env.
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True
# Scheme-qualified, e.g. https://api.ripplenovate.com
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

# Frontend base URL (used to build verification / reset links in emails).
FRONTEND_URL = env("FRONTEND_URL")
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default="Ripple Innovation Labs <no-reply@ripplenovate.com>"
)

# Paystack
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_PUBLIC_KEY = env("PAYSTACK_PUBLIC_KEY", default="")
PAYSTACK_CURRENCY = env("PAYSTACK_CURRENCY", default="NGN").upper()
USD_TO_NGN_RATE = env("USD_TO_NGN_RATE")
PAYSTACK_FEE_PERCENT = env("PAYSTACK_FEE_PERCENT")
