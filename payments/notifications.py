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


def _settler_emails(withdrawal):
    """Who needs to know a payout is waiting.

    Was every delivery lead on the platform, so one expert's request landed in
    the inbox of leads who had never worked with them — the request itself
    tells you what somebody earns, which isn't general information.

    Now: the admins who run payouts, plus the requester's own delivery lead if
    they have one. Any approved lead can still settle a request from the payout
    queue on the earnings page; this is about who gets told, not who may act.
    """
    emails = set(
        User.objects.filter(is_superuser=True, is_active=True)
        .values_list("email", flat=True)
    )
    own_lead = withdrawal.user.lead
    if own_lead and own_lead.role == User.Role.DELIVERY_LEAD:
        emails.add(own_lead.email)
    # Nobody settles their own request, so nobody needs telling about it twice.
    emails.discard(withdrawal.user.email)
    return sorted(emails)


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
    recipients = _settler_emails(withdrawal)
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


def _admin_emails():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return [u.email for u in User.objects.filter(is_superuser=True, is_active=True)]


@_safe
def notify_refund_raised(refund):
    """Tell the client money is coming back, and admins if it needs a decision.

    The client is told either way. A refund that has been raised but is waiting
    on an internal approval is still news they'd rather have than not — silence
    while a decision is pending is how a resolved complaint turns into a
    chargeback.
    """
    project = refund.project
    pending = refund.status == refund.Status.REQUESTED
    if pending:
        send_brand_email(
            subject=f"Refund needs approval: {project.code}",
            to=_admin_emails(),
            heading="A refund is waiting on you",
            paragraphs=[
                f"{refund.requested_by and (refund.requested_by.full_name or refund.requested_by.email)} "
                f"has raised a refund of {_usd(refund.amount_usd)} on "
                f"“{project.title}” ({project.code}).",
                f"Reason given: “{refund.reason}”",
                "It's above the amount a delivery lead can issue on their own, "
                "so it won't go anywhere until you approve it.",
            ],
        )
    send_brand_email(
        subject=f"About your refund: {project.title}",
        to=project.client.email,
        heading="We're refunding you" if not pending else "Your refund is being processed",
        paragraphs=[
            f"Hi {project.client.full_name or 'there'},",
            f"A refund of {_usd(refund.amount_usd)} has been raised on "
            f"“{project.title}”.",
            ("It's going through an internal approval and you'll hear from us "
             "again as soon as it's on its way."
             if pending else
             "It's on its way back to the card or account you paid from. "
             "Depending on your bank this can take a few working days."),
        ],
    )


@_safe
def notify_refund_decided(refund):
    """The outcome of an admin's decision, to the client and the lead."""
    project = refund.project
    recipients = {project.client.email}
    if project.lead_id and project.lead:
        recipients.add(project.lead.email)
    if refund.status == refund.Status.REJECTED:
        send_brand_email(
            subject=f"Refund not approved: {project.code}",
            to=sorted(recipients),
            heading="That refund wasn't approved",
            paragraphs=[
                f"The {_usd(refund.amount_usd)} refund raised on “{project.title}” "
                "has not been approved.",
                f"Reason: “{refund.failure_reason or 'No reason recorded.'}”",
            ],
        )
        return
    if refund.status == refund.Status.FAILED:
        send_brand_email(
            subject=f"Refund failed: {project.code}",
            to=sorted(recipients),
            heading="A refund didn't go through",
            paragraphs=[
                f"The {_usd(refund.amount_usd)} refund on “{project.title}” was "
                "approved but the payment provider refused it.",
                f"What they said: “{refund.failure_reason}”",
                "Nothing has left the platform, so it can be retried.",
            ],
        )
        return
    send_brand_email(
        subject=f"Your refund is on its way: {project.title}",
        to=sorted(recipients),
        heading="Refund approved",
        paragraphs=[
            f"The {_usd(refund.amount_usd)} refund on “{project.title}” has been "
            "approved and sent.",
            "Depending on the bank it can take a few working days to appear.",
        ],
    )
