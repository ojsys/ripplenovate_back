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


def _store_in_app(subject, recipients, paragraphs, cta):
    """Mirror an outgoing email into each recipient's notification bell.

    Done here rather than at the ~25 call sites on purpose: the two channels
    then share one recipient list by construction, so it is not possible to
    add a notification that emails somebody without also reaching their bell.
    A test asserts the parity; this is what makes it true.

    Silently skips addresses with no account — an invitation goes to somebody
    who doesn't exist here yet, and there is nowhere to put a bell for them.
    """
    try:
        from accounts.models import Notification, User

        body = next((p for p in paragraphs if p), "")
        url = ""
        if cta:
            _, link = cta
            # Store the path, not the absolute URL: the bell links within the
            # app, and FRONTEND_URL differs between environments.
            url = link.replace(settings.FRONTEND_URL.rstrip("/"), "", 1) or "/"
        rows = [
            Notification(user=user, title=subject, body=body, url=url)
            for user in User.objects.filter(email__in=recipients, is_active=True)
        ]
        if rows:
            Notification.objects.bulk_create(rows)
    except Exception as exc:  # never let the bell break the email
        logger.error("In-app notification failed (%s): %s", subject, exc)


def send_brand_email(subject, to, heading, paragraphs, cta=None,
                     fail_silently=True, notify=True):
    """`to` may be a single address or an iterable. `cta` is an optional (label, url).

    `notify=False` for mail that has no in-app counterpart — verifying an
    address, resetting a password, inviting somebody who has no account yet.
    A bell entry saying "confirm your email" is meaningless to someone already
    signed in, and there is nobody to show one to before they are.
    """
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

    if notify:
        _store_in_app(subject, recipients, paragraphs, cta)

    text_content = render_to_string("emails/brand_email.txt", context)
    html_content = render_to_string("emails/brand_email.html", context)

    # One message per recipient, never one message addressed to all of them.
    # A shared To: header shows every client, expert and lead each other's
    # address — a disclosure nobody agreed to, and the kind that can't be taken
    # back once sent. One failure doesn't stop the rest.
    for recipient in recipients:
        try:
            msg = EmailMultiAlternatives(
                subject, text_content, settings.DEFAULT_FROM_EMAIL, [recipient]
            )
            msg.attach_alternative(html_content, "text/html")
            msg.send()
        except Exception as exc:  # never let email break the request
            logger.error("Email send failed (%s -> %s): %s", subject, recipient, exc)
            if not fail_silently:
                raise
