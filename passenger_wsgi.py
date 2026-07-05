"""Passenger entry point for cPanel's "Setup Python App".

cPanel starts the app by importing `application` from this file. Django's own
WSGI module (ripple/wsgi.py) sets DJANGO_SETTINGS_MODULE, so we just re-export
its `application`. Keeping this file at the backend root lets you point the
cPanel "Application startup file" at `passenger_wsgi.py` and the "Application
Entry point" at `application`.
"""
import os
import sys

# Ensure this directory is importable (Passenger usually handles this, but be safe).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ripple.settings")

from ripple.wsgi import application  # noqa: E402
