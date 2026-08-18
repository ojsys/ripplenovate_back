"""Which rail carries a charge, and the one interface the views talk to.

Two collection rails now exist and the code calling them should not have to
care. Everything above this module asks for "a payment on this project" and
gets one; the choice of Paystack or Stripe is made here, from the buyer's
currency and nothing more surprising than that.

Routing, in order:

1. **The currency decides.** NGN can only go through Paystack; GBP and EUR can
   only go through Stripe. There is no judgement to make.
2. **USD is the overlap**, so the platform default breaks the tie. A Nigerian
   client paying in dollars should stay on Paystack; a US enterprise should not
   be sent to a rail their card issuer may decline.
3. **An unconfigured rail is never chosen.** With no Stripe keys the platform
   behaves exactly as it did before Stripe existed, which is what makes this
   safe to deploy ahead of having an account.

Refunds deliberately do *not* route through here — they go back down whichever
rail the original payment recorded, because that's the only place the money can
return to.
"""
import logging

from django.conf import settings

from . import paystack, stripe_gateway

logger = logging.getLogger("ripple")

PAYSTACK = "paystack"
STRIPE = stripe_gateway.NAME

# What each rail can collect. Paystack's list is the account-dependent set the
# platform actually uses; Stripe's is in its own module.
PAYSTACK_CURRENCIES = {"NGN", "USD", "GHS", "KES", "ZAR"}


class UnsupportedCurrency(Exception):
    """No configured rail can bill this buyer in the currency they want."""


def default_currency():
    return (settings.PAYSTACK_CURRENCY or "USD").upper()


def currency_for(project):
    """What this project's buyer gets charged in.

    Read off the organisation, so a company sets it once rather than per brief.
    Falls back to the platform's own setting, which is what every project used
    before organisations existed.
    """
    org = getattr(project, "organisation", None)
    preferred = (getattr(org, "preferred_currency", "") or "").upper()
    return preferred or default_currency()


def available():
    """The rails that are actually configured right now."""
    rails = {PAYSTACK: bool(settings.PAYSTACK_SECRET_KEY)}
    rails[STRIPE] = stripe_gateway.enabled()
    return {name for name, ok in rails.items() if ok}


def choose(currency):
    """The rail for a currency, or raise if nothing configured can carry it."""
    currency = (currency or "USD").upper()
    live = available()

    can_paystack = currency in PAYSTACK_CURRENCIES and PAYSTACK in live
    can_stripe = currency in stripe_gateway.CURRENCIES and STRIPE in live

    if can_paystack and can_stripe:
        # The overlap — USD, in practice. Deliberately the platform's
        # collection setting rather than "whichever is cheaper": the right
        # answer depends on where the buyer's card was issued, and guessing
        # that from a currency code would be worse than a stated default.
        preferred = (settings.PAYSTACK_CURRENCY or "").upper()
        return PAYSTACK if currency == preferred else STRIPE
    if can_paystack:
        return PAYSTACK
    if can_stripe:
        return STRIPE

    raise UnsupportedCurrency(
        f"No payment provider is set up to charge in {currency}."
    )


def module_for(name):
    return stripe_gateway if name == STRIPE else paystack


def initialize(project, user, change_order=None):
    """Start a payment on whichever rail suits this buyer.

    The one function the views call. Both rails answer with the same shape, so
    the frontend branches on `gateway` for *how* to open it — popup or redirect
    — and on nothing else.
    """
    currency = currency_for(project)
    name = choose(currency)
    if name == STRIPE:
        return stripe_gateway.initialize(
            project, user, change_order=change_order, currency=currency)
    # Paystack derives its own currency from settings, as it always has.
    result = paystack.initialize(project, user, change_order=change_order)
    result.setdefault("gateway", PAYSTACK)
    return result


def verify(reference):
    """Confirm a payment, on whichever rail it was made.

    Looked up from the recorded payment rather than guessed, so a reference
    can't be verified against the wrong provider.
    """
    from .models import Payment

    payment = Payment.objects.filter(reference=reference).first()
    if payment and payment.gateway == STRIPE:
        return stripe_gateway.verify(reference)
    return paystack.verify(reference)


def refund(payment, amount_usd):
    """Send money back the way it came. Never re-routed."""
    if payment.gateway == STRIPE:
        return stripe_gateway.create_refund(payment, amount_usd)
    return paystack.create_refund(payment, amount_usd)


def refunds_enabled(payment):
    if payment.gateway == STRIPE:
        return stripe_gateway.enabled()
    return paystack.refunds_enabled()
