"""Thin Paystack API wrapper + currency/amount logic.

All amounts are derived server-side from the stored project quote so the client
can never tamper with what it pays. Quotes live in USD; the actual charge currency
is configurable (NGN or USD) via settings.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings
from django.utils import timezone

from accounts.models import SiteSettings

from .models import Payment

PAYSTACK_BASE = "https://api.paystack.co"


class PaystackError(Exception):
    pass


def _headers():
    key = settings.PAYSTACK_SECRET_KEY
    if not key or key.startswith("sk_test_xxxx"):
        raise PaystackError(
            "Paystack secret key is not configured. Add PAYSTACK_SECRET_KEY to backend/.env."
        )
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quote_breakdown(project):
    """Return the invoice breakdown (all in USD) shown to the client."""
    subtotal = _money(project.quote_usd)
    fee_pct = Decimal(str(settings.PAYSTACK_FEE_PERCENT))
    fee = _money(subtotal * fee_pct / Decimal(100))
    total = _money(subtotal + fee)
    return {"subtotal_usd": subtotal, "fee_usd": fee, "total_usd": total}


def change_order_breakdown(change_order):
    """The invoice for extra scope. Same fee treatment as the quote."""
    subtotal = _money(change_order.amount_usd)
    fee_pct = Decimal(str(settings.PAYSTACK_FEE_PERCENT))
    fee = _money(subtotal * fee_pct / Decimal(100))
    return {"subtotal_usd": subtotal, "fee_usd": fee, "total_usd": _money(subtotal + fee)}


def usd_to_ngn_rate():
    """The rate to bill at — admin-editable in Site settings, .env as fallback."""
    return SiteSettings.usd_to_ngn()


def charge_amount(total_usd):
    """Convert a USD total into the charge currency's smallest unit."""
    currency = settings.PAYSTACK_CURRENCY
    if currency == "NGN":
        naira = _money(Decimal(total_usd) * usd_to_ngn_rate())
        return currency, int((naira * 100).to_integral_value(rounding=ROUND_HALF_UP))
    # USD (or any account-supported currency billed in cents)
    return currency, int((_money(total_usd) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def initialize(project, user, change_order=None):
    """Create a pending Payment and initialize a Paystack transaction.

    With a `change_order`, this bills the extra scope instead of the quote —
    same processing fee, same currency conversion, same webhook path. Keeping
    it one function means a change order can never be charged by a slightly
    different route than the invoice it sits beside.
    """
    breakdown = (change_order_breakdown(change_order) if change_order
                 else quote_breakdown(project))
    currency, amount_subunit = charge_amount(breakdown["total_usd"])
    reference = f"RIL-{uuid.uuid4().hex[:12].upper()}"

    resp = requests.post(
        f"{PAYSTACK_BASE}/transaction/initialize",
        headers=_headers(),
        json={
            "email": user.email,
            "amount": amount_subunit,
            "currency": currency,
            "reference": reference,
            "metadata": {
                "project_id": project.id,
                "project_code": project.code,
                "usd_total": str(breakdown["total_usd"]),
                "change_order_id": change_order.id if change_order else None,
            },
        },
        timeout=20,
    )
    data = resp.json()
    if not resp.ok or not data.get("status"):
        raise PaystackError(data.get("message", "Could not initialize payment."))

    body = data["data"]
    payment = Payment.objects.create(
        project=project,
        change_order=change_order,
        reference=reference,
        access_code=body.get("access_code", ""),
        amount_subunit=amount_subunit,
        currency=currency,
        usd_total=breakdown["total_usd"],
        usd_to_ngn_rate=usd_to_ngn_rate() if currency == "NGN" else None,
        status=Payment.Status.PENDING,
    )
    return {
        "reference": reference,
        "access_code": payment.access_code,
        "authorization_url": body.get("authorization_url"),
        "public_key": settings.PAYSTACK_PUBLIC_KEY,
        "currency": currency,
        "amount_subunit": amount_subunit,
        "usd_total": str(breakdown["total_usd"]),
    }


def _mark_paid(payment, raw):
    """Idempotently mark a payment successful and advance the project to Paid."""
    from projects import notifications  # local import to avoid cycle
    from projects.views import log_activity

    if payment.status == Payment.Status.SUCCESS:
        return payment
    payment.status = Payment.Status.SUCCESS
    payment.paid_at = timezone.now()
    payment.raw = raw
    payment.save(update_fields=["status", "paid_at", "raw"])

    project = payment.project
    if payment.change_order_id:
        # Extra scope, not the kickoff invoice: the stage doesn't move, the
        # contract value does — and with it the pool the lead has to hand out.
        order = payment.change_order
        if order.status != order.Status.PAID:
            order.status = order.Status.PAID
            order.paid_at = timezone.now()
            order.save(update_fields=["status", "paid_at"])
            log_activity(
                project, project.client,
                f"Paid for extra scope (${order.amount_usd:,.2f}): {order.description}",
            )
            notifications.notify_change_order_paid(project, order)
        return payment
    if project.stage in (project.Stage.QUOTED, project.Stage.SUBMITTED):
        project.stage = project.Stage.PAID
        project.save(update_fields=["stage"])
        log_activity(project, project.client, "Paid the invoice via Paystack.")
        notifications.notify_payment_received(project)
    return payment


def verify(reference):
    """Server-side verification against Paystack; source of truth for a payment."""
    payment = Payment.objects.select_related("project", "project__client").filter(
        reference=reference
    ).first()
    if not payment:
        raise PaystackError("Unknown payment reference.")

    resp = requests.get(
        f"{PAYSTACK_BASE}/transaction/verify/{reference}",
        headers=_headers(),
        timeout=20,
    )
    data = resp.json()
    if not resp.ok or not data.get("status"):
        raise PaystackError(data.get("message", "Verification failed."))

    body = data["data"]
    if body.get("status") == "success":
        _mark_paid(payment, body)
    elif body.get("status") in ("failed", "abandoned"):
        payment.status = Payment.Status.FAILED
        payment.raw = body
        payment.save(update_fields=["status", "raw"])
    return payment


def refunds_enabled():
    return bool(settings.PAYSTACK_REFUNDS_ENABLED)


def create_refund(payment, amount_usd):
    """Ask Paystack to return part (or all) of a charge to the client.

    Paystack refunds are keyed on the original transaction, which is why the
    caller has to hand over the `Payment` rather than just an amount — a
    project can in principle have been settled by more than one charge, and
    guessing which to reverse is not something this should do silently.

    The amount is converted at the *original* charge's rate, not today's. A
    client who paid ₦1,600,000 for $1,000 gets their naira back, not whatever
    $1,000 happens to be worth this week.
    """
    if payment.currency == "NGN":
        rate = payment.usd_to_ngn_rate or usd_to_ngn_rate()
        subunit = int((_money(Decimal(amount_usd) * rate) * 100)
                      .to_integral_value(rounding=ROUND_HALF_UP))
    else:
        subunit = int((_money(amount_usd) * 100)
                      .to_integral_value(rounding=ROUND_HALF_UP))

    resp = requests.post(
        f"{PAYSTACK_BASE}/refund",
        headers=_headers(),
        json={"transaction": payment.reference, "amount": subunit},
        timeout=30,
    )
    data = resp.json()
    if not resp.ok or not data.get("status"):
        raise PaystackError(data.get("message") or "Paystack refused the refund.")
    return data["data"]
