"""
Settings selector.

    DJANGO_ENV=production   ->  prod.py
    anything else / unset   ->  dev.py

Every entry point (manage.py, ripple/wsgi.py, passenger_wsgi.py) points at
``ripple.settings``; this package decides which concrete module to load, so you
never pass ``--settings``. Set ``DJANGO_ENV`` in your ``.env`` (or the cPanel
Python App's environment variables).
"""
import os
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


def _detect_environment():
    """A real environment variable wins; otherwise peek DJANGO_ENV from .env.

    We read DJANGO_ENV directly (rather than via read_env) so the choice is made
    before any .env loading and can't be clobbered.
    """
    value = os.environ.get("DJANGO_ENV")
    if not value:
        env_file = _BACKEND_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("DJANGO_ENV") and "=" in line:
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    break
    return (value or "development").strip().lower()


DJANGO_ENV = _detect_environment()

if DJANGO_ENV in ("production", "prod"):
    from .prod import *  # noqa: F401,F403
else:
    from .dev import *  # noqa: F401,F403
