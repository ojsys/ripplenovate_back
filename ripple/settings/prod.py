"""
Production settings — active when DJANGO_ENV=production.

DEBUG off, database from DATABASE_URL (MySQL on cPanel, or Postgres), static
files hashed + compressed and served by WhiteNoise, SMTP email, and HTTPS
hardening. All deployment values come from the environment / .env.
"""
import os

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, env

DEBUG = False

# Required in production, e.g. ALLOWED_HOSTS=api.ripplenovate.com
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Database from DATABASE_URL — mysql://… (cPanel) or postgres://…
DATABASES = {"default": env.db("DATABASE_URL")}

# Hash + gzip static files for cache-busting; WhiteNoise serves them.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# SMTP email (so verification / reset emails actually send).
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)

# HTTPS / security. TLS terminates at cPanel/Apache, so trust the proxy header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"


# ---------------------------------------------------------------------------
# Logging — since DEBUG is off, errors don't show in the browser. This writes
# them to a rotating file (backend/logs/ripple.log) AND to the console (which
# Passenger captures into the app's stderr.log). Tail either to debug prod.
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
_handlers = ["console"]
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _file_handler = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(LOG_DIR / "ripple.log"),
        "maxBytes": 5 * 1024 * 1024,  # 5 MB per file
        "backupCount": 5,             # keep 5 rotated files
        "formatter": "verbose",
        "level": LOG_LEVEL,
    }
    _handlers.append("file")
except OSError:
    _file_handler = None  # not writable — fall back to console only

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose", "level": LOG_LEVEL},
        **({"file": _file_handler} if _file_handler else {}),
    },
    "root": {"handlers": _handlers, "level": LOG_LEVEL},
    "loggers": {
        # 500-level request errors, with tracebacks, land here.
        "django.request": {"handlers": _handlers, "level": "ERROR", "propagate": False},
        # Your own app messages: logging.getLogger("ripple").error(...)
        "ripple": {"handlers": _handlers, "level": LOG_LEVEL, "propagate": False},
    },
}
