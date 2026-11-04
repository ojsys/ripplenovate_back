"""Paystack Transfers — actually moving a payout to an earner's bank account.

Flow, per Paystack's API:

    1. resolve  — check the account number really belongs to that name
    2. recipient — register the account once, reuse the recipient code after
    3. transfer  — send the money; Paystack answers `success`, `pending`, or `otp`
    4. confirm   — a verify call is the source of truth for the final state.
                   We don't receive transfer webhooks (the Paystack account is
                   shared with other products, and it has one webhook URL), so
                   `reconcile` asks — on a schedule, and whenever anyone looks.

A withdrawal is only marked paid on Paystack's confirmation, never on the fact
that we asked. Anything that fails puts the amount back in the earner's balance.
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import Withdrawal
from .paystack import PAYSTACK_BASE, PaystackError, _headers

logger = logging.getLogger("ripple")

# Paystack recipient types by currency. NGN accounts are `nuban`.
RECIPIENT_TYPES = {"NGN": "nuban", "GHS": "mobile_money", "KES": "mobile_money",
                   "ZAR": "basa", "USD": "nuban"}
BANK_CACHE_KEY = "ril:paystack:banks:{}"
BANK_CACHE_TTL = 60 * 60 * 24  # bank lists barely change

# What the earner sees on their bank statement. The payout reference travels
# separately as the transfer's `reference`, so this stays clean and recognisable.
TRANSFER_NARRATION = "RIPPLENOVATE LAB Withdrawal"


def transfers_enabled():
    return bool(settings.PAYSTACK_TRANSFERS_ENABLED)


def _post(path, payload):
    resp = requests.post(f"{PAYSTACK_BASE}{path}", headers=_headers(), json=payload, timeout=30)
    data = resp.json()
    if not resp.ok or not data.get("status"):
        raise PaystackError(data.get("message") or "Paystack rejected that request.")
    return data["data"]


def _get(path):
    resp = requests.get(f"{PAYSTACK_BASE}{path}", headers=_headers(), timeout=30)
    data = resp.json()
    if not resp.ok or not data.get("status"):
        raise PaystackError(data.get("message") or "Paystack rejected that request.")
    return data["data"]


def list_banks(currency=None):
    """Banks Paystack can pay into, cached — the list is long and static."""
    currency = currency or settings.PAYSTACK_CURRENCY
    key = BANK_CACHE_KEY.format(currency)
    cached = cache.get(key)
    if cached:
        return cached
    country = {"NGN": "nigeria", "GHS": "ghana", "ZAR": "south africa",
               "KES": "kenya"}.get(currency, "nigeria")
    data = _get(f"/bank?country={country}&currency={currency}&perPage=100")
    banks = [
        {"name": b["name"], "code": b["code"], "slug": b.get("slug", "")}
        for b in data if b.get("code")
    ]
    banks.sort(key=lambda b: b["name"])
    cache.set(key, banks, BANK_CACHE_TTL)
    return banks


def resolve_account(account_number, bank_code):
    """Ask Paystack whose account this is, so payouts can't go to a typo."""
    data = _get(f"/bank/resolve?account_number={account_number}&bank_code={bank_code}")
    return {
        "account_number": data.get("account_number", account_number),
        "account_name": data.get("account_name", ""),
    }


def ensure_recipient(user):
    """Register the user's bank account with Paystack once; reuse it after.

    The stored code is cleared whenever the account details change (see the
    payout-account endpoint), so this can't send money to a stale account.
    """
    if user.paystack_recipient_code:
        return user.paystack_recipient_code
    if not user.has_payout_account:
        raise PaystackError(
            "Add your bank account in profile settings before requesting a payout."
        )
    currency = settings.PAYSTACK_CURRENCY
    data = _post("/transferrecipient", {
        "type": RECIPIENT_TYPES.get(currency, "nuban"),
        "name": user.bank_account_name,
        "account_number": user.bank_account_number,
        "bank_code": user.bank_code,
        "currency": currency,
    })
    code = data.get("recipient_code", "")
    if not code:
        raise PaystackError("Paystack did not return a recipient for that account.")
    user.paystack_recipient_code = code
    user.save(update_fields=["paystack_recipient_code"])
    return code


def _apply_transfer_state(withdrawal, body, actor=None):
    """Map a Paystack transfer payload onto the withdrawal. Idempotent."""
    state = (body.get("status") or "").lower()
    withdrawal.transfer_code = body.get("transfer_code", withdrawal.transfer_code)
    withdrawal.transfer_reference = body.get("reference", withdrawal.transfer_reference)
    withdrawal.transfer_raw = body

    if state == "success":
        # Re-confirming a payout already paid keeps the time it first landed;
        # moving it would rewrite history, and keep the scheduled sync's
        # reversal window from ever closing on it.
        if withdrawal.status != Withdrawal.Status.PAID or not withdrawal.processed_at:
            withdrawal.processed_at = timezone.now()
        withdrawal.status = Withdrawal.Status.PAID
        withdrawal.failure_reason = ""
    elif state in ("failed", "reversed", "abandoned"):
        withdrawal.status = Withdrawal.Status.FAILED
        withdrawal.failure_reason = (
            body.get("gateway_response") or body.get("message")
            or f"Paystack reported the transfer {state}."
        )[:255]
    else:
        # pending / otp / queued — money is in flight, not landed.
        withdrawal.status = Withdrawal.Status.PROCESSING
        if state == "otp":
            withdrawal.failure_reason = "Awaiting OTP confirmation on your Paystack account."

    if actor is not None:
        withdrawal.processed_by = actor
    withdrawal.save()
    return withdrawal


def send(withdrawal, actor=None):
    """Initiate the payout. Returns the withdrawal with its new state.

    Raises PaystackError (leaving the withdrawal untouched) when Paystack won't
    accept the request at all — a bad account, transfers disabled on the
    business, or an insufficient balance.
    """
    if withdrawal.status in Withdrawal.FINAL_STATUSES:
        raise PaystackError(
            f"This payout is already {withdrawal.get_status_display().lower()}."
        )
    user = withdrawal.user
    recipient = ensure_recipient(user)

    body = _post("/transfer", {
        "source": "balance",
        "amount": withdrawal.amount_subunit,
        "recipient": recipient,
        "currency": withdrawal.currency,
        "reason": TRANSFER_NARRATION,
        "reference": withdrawal.reference,
    })
    withdrawal.recipient_code = recipient
    # Keep the payout account in step with what was actually paid.
    withdrawal.bank_code = withdrawal.bank_code or user.bank_code
    return _apply_transfer_state(withdrawal, body, actor=actor)


def verify(withdrawal):
    """Re-read a transfer from Paystack — the answer wins over local state."""
    if not (withdrawal.transfer_reference or withdrawal.transfer_code):
        raise PaystackError("This payout hasn't been sent through Paystack.")
    ref = withdrawal.transfer_reference or withdrawal.reference
    body = _get(f"/transfer/verify/{ref}")
    return _apply_transfer_state(withdrawal, body)


# How often one payout may be re-checked with Paystack on its own initiative.
# Every Earnings page load reconciles, so without a floor a lead refreshing
# would hammer the verify endpoint.
RECONCILE_INTERVAL_SECONDS = 20
RECONCILE_CACHE_KEY = "ril:payout:reconciled:{}"

# How far back the scheduled sync re-checks payouts already marked paid. A
# transfer Paystack reported as successful can still be reversed by the bank
# a day or two later; with no webhook, asking again is the only way to hear.
REVERSAL_WINDOW_DAYS = 3


def reconcile(withdrawals, throttle=True):
    """Ask Paystack where each payout stands, and apply the answer. Never raises.

    **This is how payouts settle.** The Paystack account is shared with other
    products, and Paystack allows one webhook URL per account, so this platform
    does not receive transfer webhooks at all. Verify is the same source of
    truth asked directly: the scheduled `sync_payouts` job runs this every few
    minutes, every Earnings page load runs it for what's on screen, and the
    settle modal polls it while the lead watches.

    Only payouts that have actually been handed to Paystack are asked about.
    `throttle` stops page refreshes re-asking about the same payout within
    RECONCILE_INTERVAL_SECONDS; the scheduled job passes False.

    Returns the withdrawals whose status changed. Notifies the earner for any
    that settled or failed.
    """
    from . import notifications  # local: notifications imports models

    changed = []
    for w in withdrawals:
        if w.status not in (Withdrawal.Status.PROCESSING, Withdrawal.Status.PAID):
            continue
        if not (w.transfer_reference or w.transfer_code):
            continue  # recorded by hand, or never sent — nothing to ask
        if throttle and not cache.add(RECONCILE_CACHE_KEY.format(w.pk), 1,
                                      RECONCILE_INTERVAL_SECONDS):
            continue  # checked moments ago
        before = w.status
        try:
            verify(w)
        except Exception as exc:  # noqa: BLE001 — one bad payout mustn't stop the rest
            logger.warning("payout reconcile for %s failed: %s", w.reference, exc)
            continue
        if w.status != before:
            changed.append(w)
            logger.info("payout %s: %s -> %s", w.reference, before, w.status)
            if w.status != Withdrawal.Status.PROCESSING:
                notifications.notify_withdrawal_settled(w)
    return changed


def sync_all():
    """The scheduled job's whole body: every payout still in flight, plus
    recently paid ones in case the bank reversed them."""
    since = timezone.now() - timedelta(days=REVERSAL_WINDOW_DAYS)
    in_flight = Withdrawal.objects.filter(status=Withdrawal.Status.PROCESSING)
    recent = Withdrawal.objects.filter(status=Withdrawal.Status.PAID,
                                       processed_at__gte=since)
    rows = list((in_flight | recent).select_related("user").order_by("id"))
    return rows, reconcile(rows, throttle=False)


def finalize(withdrawal, otp):
    """Complete a transfer that Paystack held for an OTP."""
    if not withdrawal.transfer_code:
        raise PaystackError("This payout hasn't been sent through Paystack.")
    body = _post("/transfer/finalize_transfer", {
        "transfer_code": withdrawal.transfer_code,
        "otp": otp,
    })
    return _apply_transfer_state(withdrawal, body)


def handle_webhook_event(event, data):
    """Apply a transfer.* webhook to the matching withdrawal.

    Paystack echoes our own `reference`, so this matches without trusting any
    other field in the payload.
    """
    reference = data.get("reference")
    transfer_code = data.get("transfer_code")
    withdrawal = Withdrawal.objects.select_related("user").filter(
        reference=reference
    ).first()
    if not withdrawal and transfer_code:
        withdrawal = Withdrawal.objects.select_related("user").filter(
            transfer_code=transfer_code
        ).first()
    if not withdrawal:
        logger.warning("transfer webhook %s for unknown payout %s", event, reference)
        return None
    return _apply_transfer_state(withdrawal, data)
