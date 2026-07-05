"""
Development settings — active when DJANGO_ENV is unset or 'development'.

DEBUG on, local SQLite database, emails printed to the console. No secrets or
external services required to run locally.
"""
from .base import *  # noqa: F401,F403
from .base import BASE_DIR

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]

# Zero-setup local database.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Verification / reset emails print to the runserver console in development.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
