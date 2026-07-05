"""
Production settings — active when DJANGO_ENV=production.

DEBUG off, database from DATABASE_URL (MySQL on cPanel, or Postgres), static
files hashed + compressed and served by WhiteNoise, SMTP email, and HTTPS
hardening. All deployment values come from the environment / .env.
"""
from .base import *  # noqa: F401,F403
from .base import env

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
