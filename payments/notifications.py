"""Earnings + payout email notifications.

Same contract as projects.notifications: a notification failure is logged, never
raised — an email must not break the payout it describes.
"""
import functools
import logging

from django.conf import settings
from django.contrib.auth import get_user_model

from ripple.mailer import send_brand_email

from .models import Withdrawal

User = get_user_model()
logger = logging.getLogger("ripple")


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.error("notify %s failed: %s", fn.__name__, exc)

    return wrapper


def _earnings_url():
    return f"{settings.FRONTEND_URL.rstrip('/')}/earnings"


def _usd(amount):
    return "${:,.2f}".format(amount or 0)


def _lead_emails(exclude=None):
    emails = set(
        User.objects.filter(role=User.Role.DELIVERY_LEAD).values_list("email", flat=True)
    )
    emails.discard(exclude)
    return list(emails)


def _first_name(user):
    return (user.full_name or "").split(" ")[0] or "there"


@_safe
def notify_earning_credited(user, project, amount_usd):
    send_brand_email(
        subject=f"You've earned {_usd(amount_usd)}",
        to=user.email,
        heading=f"{_usd(amount_usd)} added to your earnings",
        paragraphs=[
            f"Hi {_first_name(user)},",
            f"“{project.title}” was approved by the client, so your share of the project "
            f"— {_usd(amount_usd)} — is now available to withdraw.",
        ],
        cta=("View my earnings", _earnings_url()),
    )


@_safe
def notify_task_earning_credited(task, amount_usd):
    """One task signed off, one payment released.

    Separate from `notify_earning_credited` because the reason differs: this one
    lands mid-project, on the lead's approval of a specific piece of work, not
    on the client closing the whole thing out. Saying "the client approved your
    project" here would be untrue and would set the wrong expectation about
    what's left to do.
    """
    if not task.assignee:
        return
    send_brand_email(
        subject=f"You've earned {_usd(amount_usd)}",
        to=task.assignee.email,
        heading=f"{_usd(amount_usd)} added to your earnings",
        paragraphs=[
            f"Hi {_first_name(task.assignee)},",
            f"“{task.title}” on “{task.project.title}” was approved by your "
            f"delivery lead, so {_usd(amount_usd)} is now available to withdraw.",
        ],
        cta=("View my earnings", _earnings_url()),
    )


@_safe
def notify_withdrawal_requested(withdrawal):
    """Confirm to the earner, and queue it up for whoever settles payouts."""
    send_brand_email(
        subject=f"Withdrawal requested · {_usd(withdrawal.amount_usd)}",
        to=withdrawal.user.email,
        heading="We've got your withdrawal request",
        paragraphs=[
            f"Hi {_first_name(withdrawal.user)},",
            f"You requested {_usd(withdrawal.amount_usd)} to {withdrawal.bank_name} "
            f"({withdrawal.masked_account}).",
            f"Reference {withdrawal.reference}. We'll email you again the moment it's paid.",
        ],
        cta=("Track this payout", _earnings_url()),
    )
    recipients = _lead_emails(exclude=withdrawal.user.email)
    if recipients:
        send_brand_email(
            subject=f"Payout request: {_usd(withdrawal.amount_usd)}",
            to=recipients,
            heading="A payout request needs settling",
            paragraphs=[
                f"{withdrawal.user.full_name or withdrawal.user.email} "
                f"({withdrawal.user.role_label}) requested {_usd(withdrawal.amount_usd)}.",
                f"Pay to {withdrawal.bank_account_name} · {withdrawal.bank_name} · "
                f"{withdrawal.bank_account_number}, then mark it paid.",
            ],
            cta=("Open payout requests", _earnings_url()),
        )


@_safe
def notify_withdrawal_settled(withdrawal):
    if withdrawal.status == Withdrawal.Status.PAID:
        heading = f"{_usd(withdrawal.amount_usd)} is on its way"
        paragraphs = [
            f"Hi {_first_name(withdrawal.user)},",
            f"Your withdrawal of {_usd(withdrawal.amount_usd)} has been paid to "
            f"{withdrawal.bank_name} ({withdrawal.masked_account}).",
        ]
    elif withdrawal.status == Withdrawal.Status.REJECTED:
        heading = "We couldn't process that withdrawal"
        paragraphs = [
            f"Hi {_first_name(withdrawal.user)},",
            f"Your request for {_usd(withdrawal.amount_usd)} was declined, and the amount "
            "is back in your available balance.",
        ]
    else:
        heading = "Your withdrawal is being processed"
        paragraphs = [
            f"Hi {_first_name(withdrawal.user)},",
            f"Your withdrawal of {_usd(withdrawal.amount_usd)} is being transferred now.",
        ]
    if withdrawal.note:
        paragraphs.append(f"Note: {withdrawal.note}")
    send_brand_email(
        subject=f"Withdrawal {withdrawal.get_status_display().lower()} · {withdrawal.reference}",
        to=withdrawal.user.email,
        heading=heading,
        paragraphs=paragraphs,
        cta=("View my earnings", _earnings_url()),
    )
