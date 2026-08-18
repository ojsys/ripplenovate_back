"""Stripe — collecting from clients Paystack can't bill.

**Collection only.** Payouts stay entirely on Paystack, which is the right rail
for African talent and is not moving. Nothing here touches a withdrawal.

Two deliberate choices:

*Checkout, not Elements.* A hosted Checkout Session is a URL to redirect to.
No JavaScript SDK, no card details ever reaching this application, no new
script tag to argue with a content-security policy about. The client comes back
to a return URL and the webhook is the authority on whether they actually paid.

*`requests`, not the Stripe SDK.* Consistent with `paystack.py` next door, which
has always spoken HTTP directly, and it keeps the deployment a `pip install`
lighter. Stripe's API is form-encoded rather than JSON, which is the only real
difference in shape.

The invariant that does **not** change: the whole quote is collected before work
starts. That is what makes "the expert is paid from money already in the
building" true, and it is why invoicing on terms was deferred rather than built
here.
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings
from django.utils import timezone

from .models import Payment

logger = logging.getLogger("ripple")

STRIPE_BASE = "https://api.stripe.com/v1"
NAME = "stripe"

# What this rail can collect. Deliberately a short, explicit list rather than
# "everything Stripe supports": each currency here is one somebody has decided
# the platform will quote and reconcile in.
CURRENCIES = {"USD", "GBP", "EUR", "CAD", "AUD"}

# How far out of date a webhook may be before it's treated as a replay rather
# than a slow delivery. Stripe's own recommendation.
SIGNATURE_TOLERANCE_SECONDS = 300


class StripeError(Exception):
    pass


def enabled():
    return bool(settings.STRIPE_SECRET_KEY)


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _headers():
    key = settings.STRIPE_SECRET_KEY
    if not key:
        raise StripeError(
            "Stripe is not configured. Add STRIPE_SECRET_KEY to backend/.env."
        )
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _post(path, data):
    resp = requests.post(f"{STRIPE_BASE}{path}", headers=_headers(),
                         data=data, timeout=30)
    body = resp.json()
    if not resp.ok:
        message = (body.get("error") or {}).get("message")
        raise StripeError(message or "Stripe rejected that request.")
    return body


def _get(path):
    resp = requests.get(f"{STRIPE_BASE}{path}", headers=_headers(), timeout=30)
    body = resp.json()
    if not resp.ok:
        message = (body.get("error") or {}).get("message")
        raise StripeError(message or "Stripe rejected that request.")
    return body


def charge_amount(total_usd, currency):
    """USD into the charge currency's smallest unit.

    USD passes straight through. Anything else is billed at Stripe's own
    conversion — the platform does not hold a rate table for five currencies
    and pretend it's accurate. The client sees the converted amount on
    Stripe's page before they confirm.
    """
    return int((_money(total_usd) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def initialize(project, user, change_order=None, currency="USD"):
    """Open a Checkout Session and record the pending payment.

    Mirrors `paystack.initialize` down to the return shape, so the view calling
    it doesn't know or care which rail it got.
    """
    from . import paystack

    breakdown = (paystack.change_order_breakdown(change_order) if change_order
                 else paystack.quote_breakdown(project))
    currency = (currency or "USD").upper()
    amount = charge_amount(breakdown["total_usd"], currency)
    reference = f"RIL-{uuid.uuid4().hex[:12].upper()}"
    label = (f"{project.code} · extra scope" if change_order
             else f"{project.code} · {project.title}")

    frontend = settings.FRONTEND_URL.rstrip("/")
    body = _post("/checkout/sessions", {
        "mode": "payment",
        "client_reference_id": reference,
        "customer_email": user.email,
        "success_url": f"{frontend}/projects/{project.id}?paid={reference}",
        "cancel_url": f"{frontend}/projects/{project.id}?cancelled=1",
        "line_items[0][quantity]": 1,
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][unit_amount]": amount,
        "line_items[0][price_data][product_data][name]": label[:250],
        "metadata[project_id]": project.id,
        "metadata[project_code]": project.code,
        "metadata[reference]": reference,
        "metadata[usd_total]": str(breakdown["total_usd"]),
        "metadata[change_order_id]": change_order.id if change_order else "",
    })

    payment = Payment.objects.create(
        project=project,
        change_order=change_order,
        gateway=NAME,
        reference=reference,
        # The session id, so `verify` can ask Stripe about it later. Same slot
        # Paystack's access code uses, for the same purpose.
        access_code=body.get("id", ""),
        amount_subunit=amount,
        currency=currency,
        usd_total=breakdown["total_usd"],
        status=Payment.Status.PENDING,
    )
    return {
        "gateway": NAME,
        "reference": reference,
        "access_code": payment.access_code,
        # The frontend redirects here rather than opening a popup — which is
        # why this key matters more on this rail than on Paystack's.
        "authorization_url": body.get("url"),
        "public_key": settings.STRIPE_PUBLIC_KEY,
        "currency": currency,
        "amount_subunit": amount,
        "usd_total": str(breakdown["total_usd"]),
    }


def verify(reference):
    """Ask Stripe whether a session was actually paid.

    The belt to the webhook's braces, exactly as on Paystack: the client
    returning to the success URL proves they came back, not that the charge
    cleared.
    """
    from . import paystack

    payment = Payment.objects.select_related(
        "project", "project__client", "change_order").filter(
        reference=reference, gateway=NAME).first()
    if not payment:
        raise StripeError("We don't recognise that payment reference.")
    if payment.status == Payment.Status.SUCCESS:
        return payment
    if not payment.access_code:
        raise StripeError("That payment was never started properly.")

    session = _get(f"/checkout/sessions/{payment.access_code}")
    if session.get("payment_status") == "paid":
        paystack._mark_paid(payment, session)
    return payment


def create_refund(payment, amount_usd):
    """Send money back down the rail it arrived on.

    Refunds key on the payment intent rather than the session, which is why the
    session has to be fetched first — the id isn't known at collection time.
    """
    session = _get(f"/checkout/sessions/{payment.access_code}")
    intent = session.get("payment_intent")
    if not intent:
        raise StripeError("That payment has no charge to refund.")
    amount = charge_amount(amount_usd, payment.currency)
    return _post("/refunds", {"payment_intent": intent, "amount": amount})


def verify_webhook(payload, header, secret=None):
    """Check a webhook really came from Stripe, and isn't a replay.

    Hand-rolled because there is no SDK here, and worth reading carefully: this
    is the only thing standing between a public URL and anybody being able to
    mark their own project paid.

    Stripe signs `"{timestamp}.{raw body}"` with the endpoint secret. The header
    carries the timestamp and one or more `v1=` signatures — more than one
    during a secret rotation, so any match counts. The timestamp is checked
    against a tolerance, without which a captured-and-replayed request would
    stay valid forever.
    """
    secret = secret if secret is not None else settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        raise StripeError("Stripe webhook secret is not configured.")

    parts = {}
    for chunk in (header or "").split(","):
        key, _, value = chunk.strip().partition("=")
        if key == "v1":
            parts.setdefault("v1", []).append(value)
        elif key:
            parts[key] = value

    timestamp = parts.get("t")
    signatures = parts.get("v1") or []
    if not timestamp or not signatures:
        raise StripeError("Malformed Stripe signature header.")
    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        raise StripeError("Malformed Stripe signature timestamp.")
    if age > SIGNATURE_TOLERANCE_SECONDS:
        raise StripeError("That Stripe event is too old to trust.")

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    # compare_digest against every offered signature, so a key rotation doesn't
    # drop events — and constant-time throughout.
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise StripeError("Stripe signature did not match.")

    try:
        return json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError):
        raise StripeError("Stripe sent something that isn't JSON.")


def handle_event(event):
    """Act on a verified event. Returns the payment it touched, or None."""
    from . import paystack

    name = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if name == "checkout.session.completed":
        if obj.get("payment_status") != "paid":
            # An async method (bank debit) that hasn't cleared. Wait for
            # `checkout.session.async_payment_succeeded` instead of crediting
            # work against money that may never arrive.
            return None
        reference = (obj.get("client_reference_id")
                     or (obj.get("metadata") or {}).get("reference"))
        payment = Payment.objects.select_related(
            "project", "project__client", "change_order").filter(
            reference=reference, gateway=NAME).first()
        if payment:
            paystack._mark_paid(payment, obj)
        return payment

    if name == "checkout.session.async_payment_succeeded":
        reference = (obj.get("client_reference_id")
                     or (obj.get("metadata") or {}).get("reference"))
        payment = Payment.objects.select_related(
            "project", "project__client", "change_order").filter(
            reference=reference, gateway=NAME).first()
        if payment:
            paystack._mark_paid(payment, obj)
        return payment

    return None
