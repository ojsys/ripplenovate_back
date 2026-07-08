"""Shared branded email sender used across the app.

Sends a multipart (HTML + plain-text) email with a consistent Ripple look.
All sends are guarded so a mail failure never breaks the request that triggered
it — errors are logged instead.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import escape

logger = logging.getLogger("ripple")


def brand_name():
    try:
        from accounts.models import SiteSettings

        return SiteSettings.load().brand_name
    except Exception:
        return "Ripple Innovation Labs"


def send_brand_email(subject, to, heading, paragraphs, cta=None, fail_silently=True):
    """`to` may be a single address or an iterable. `cta` is an optional (label, url)."""
    recipients = [to] if isinstance(to, str) else list(to or [])
    recipients = sorted({e for e in recipients if e})
    if not recipients:
        return

    brand = brand_name()
    # Escape variables for safe rendering
    escaped_brand = escape(brand)
    escaped_heading = escape(heading)
    escaped_paragraphs = [escape(p) for p in paragraphs]
    escaped_cta = None
    if cta:
        label, url = cta
        escaped_cta = {"label": escape(label), "url": escape(url)}
    
    context = {
        "brand": escaped_brand,
        "heading": escaped_heading,
        "paragraphs": escaped_paragraphs,
        "cta": escaped_cta,
    }

    text_content = render_to_string("emails/brand_email.txt", context)
    html_content = render_to_string("emails/brand_email.html", context)

    try:
        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, recipients)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as exc:  # never let email break the request
        logger.error("Email send failed (%s -> %s): %s", subject, recipients, exc)
        if not fail_silently:
            raise
