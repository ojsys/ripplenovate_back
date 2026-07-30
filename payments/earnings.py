"""Earnings + withdrawable balance for developers and delivery leads.

A project's quote is split on approval: the assigned developer takes one share,
the delivery lead who quoted it takes another, and the remainder stays with the
platform. Shares are admin-editable in Site settings.

Money that hasn't been approved yet is never written to the ledger — it's
projected live from active projects, so nothing is withdrawable until the client
signs the work off.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from accounts.models import SiteSettings
from projects.models import Project

from .models import Earning, Withdrawal

ZERO = Decimal("0.00")

# Stages where the client has paid but hasn't approved delivery yet: real work,
# not yet earned.
PENDING_STAGES = [Project.Stage.PAID, Project.Stage.IN_PROGRESS, Project.Stage.REVIEW]


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def config():
    return SiteSettings.payout_config()


def share_percent(kind, cfg=None):
    """The site-wide default share for a role (before per-project overrides)."""
    cfg = cfg or config()
    if kind == Earning.Kind.DEVELOPER:
        return Decimal(cfg["developer_share_percent"])
    return Decimal(cfg["delivery_lead_share_percent"])


def project_share(project, kind):
    """(percent, amount) this project pays a role — honouring its overrides."""
    split = project.payout_split()
    if kind == Earning.Kind.DEVELOPER:
        return split["developer_percent"], split["developer_usd"]
    return split["delivery_lead_percent"], split["delivery_lead_usd"]


def share_amount(quote_usd, percent):
    return _money(Decimal(quote_usd or 0) * Decimal(percent) / Decimal(100))


def _earners(project):
    """(user, kind) pairs that a project pays out to, skipping unfilled roles."""
    pairs = []
    if project.developer_id:
        pairs.append((project.developer_id, Earning.Kind.DEVELOPER))
    if project.lead_id:
        pairs.append((project.lead_id, Earning.Kind.DELIVERY_LEAD))
    return pairs


def record_project_earnings(project):
    """Write the ledger rows for an approved project. Idempotent.

    Called when a client approves delivery, and again lazily whenever earnings
    are read — so projects completed before this feature existed (or seeded)
    still show up exactly once.
    """
    if project.stage != Project.Stage.COMPLETED or not project.quote_usd:
        return []

    created = []
    for user_id, kind in _earners(project):
        percent, amount = project_share(project, kind)
        if amount <= ZERO:
            continue
        earning, was_created = Earning.objects.get_or_create(
            project=project,
            user_id=user_id,
            kind=kind,
            defaults={"share_percent": percent, "amount_usd": amount},
        )
        if was_created:
            created.append(earning)
    return created


def backfill(user):
    """Make sure every completed project this user earned on has a ledger row."""
    completed = Project.objects.filter(
        Q(developer=user) | Q(lead=user), stage=Project.Stage.COMPLETED
    ).distinct()
    for project in completed:
        record_project_earnings(project)


def _projected(user):
    """Not-yet-earned value from the user's paid-but-unapproved projects."""
    active = Project.objects.filter(
        Q(developer=user) | Q(lead=user), stage__in=PENDING_STAGES
    ).distinct()
    total = ZERO
    for project in active:
        split = project.payout_split()
        # A lead can also be the assigned developer; both shares count.
        if project.developer_id == user.id:
            total += split["developer_usd"]
        if project.lead_id == user.id:
            total += split["delivery_lead_usd"]
    return _money(total)


def _sum(queryset, field="amount_usd"):
    return _money(queryset.aggregate(total=Sum(field))["total"] or 0)


def summary(user, refresh=True):
    """Everything the earnings screen needs about one user's money."""
    if refresh:
        backfill(user)
    cfg = config()
    earned = _sum(Earning.objects.filter(user=user))
    withdrawals = Withdrawal.objects.filter(user=user)
    paid_out = _sum(withdrawals.filter(status=Withdrawal.Status.PAID))
    in_review = _sum(withdrawals.filter(status__in=Withdrawal.OPEN_STATUSES))
    available = _money(earned - paid_out - in_review)
    return {
        "earned_usd": earned,
        "paid_out_usd": paid_out,
        "requested_usd": in_review,
        "available_usd": max(available, ZERO),
        "pending_usd": _projected(user),
        "min_withdrawal_usd": _money(cfg["min_withdrawal_usd"]),
        "developer_share_percent": Decimal(cfg["developer_share_percent"]),
        "delivery_lead_share_percent": Decimal(cfg["delivery_lead_share_percent"]),
    }


def available_balance(user):
    return summary(user, refresh=False)["available_usd"]


class WithdrawalError(Exception):
    """A withdrawal request that can't be honoured (amount, balance, details)."""


def _reference():
    return f"RIL-WD-{uuid.uuid4().hex[:10].upper()}"


@transaction.atomic
def request_withdrawal(user, amount_usd, bank_name, account_number, account_name):
    """Validate a payout request against the live balance and record it.

    The balance is re-read inside the transaction so two quick requests can't
    both clear the same funds.
    """
    from .paystack import charge_amount, usd_to_ngn_rate  # local import: avoids a cycle

    if not user.can_earn:
        raise WithdrawalError("Only developers and delivery leads have earnings to withdraw.")

    amount = _money(amount_usd)
    if amount <= ZERO:
        raise WithdrawalError("Enter an amount to withdraw.")

    minimum = _money(config()["min_withdrawal_usd"])
    if amount < minimum:
        raise WithdrawalError(f"The minimum withdrawal is ${minimum}.")

    available = summary(user)["available_usd"]
    if amount > available:
        raise WithdrawalError(f"You can withdraw up to ${available} right now.")

    bank_name = (bank_name or "").strip()
    account_number = (account_number or "").strip()
    account_name = (account_name or "").strip()
    if not (bank_name and account_number and account_name):
        raise WithdrawalError("Add your bank name, account number, and account name.")

    # Keep the profile in step so the next request is prefilled.
    user.bank_name = bank_name
    user.bank_account_number = account_number
    user.bank_account_name = account_name
    user.save(update_fields=["bank_name", "bank_account_number", "bank_account_name"])

    currency, amount_subunit = charge_amount(amount)
    return Withdrawal.objects.create(
        user=user,
        reference=_reference(),
        amount_usd=amount,
        currency=currency,
        amount_subunit=amount_subunit,
        usd_to_ngn_rate=usd_to_ngn_rate() if currency == "NGN" else None,
        bank_name=bank_name,
        bank_account_number=account_number,
        bank_account_name=account_name,
        status=Withdrawal.Status.REQUESTED,
    )


def settle(withdrawal, status, actor, note=""):
    """Move a request to processing / paid / rejected on someone's behalf.

    A delivery lead can't sign off their own payout — that needs a second pair of
    eyes. Superusers are exempt: they can already edit the row in the Django
    admin, so blocking them would be a control in name only (and a lone admin
    would have no way to ever be paid).
    """
    if withdrawal.user_id == actor.id and not actor.is_superuser:
        raise WithdrawalError(
            "You can't settle your own withdrawal — ask another delivery lead or an admin."
        )
    if withdrawal.status in (Withdrawal.Status.PAID, Withdrawal.Status.REJECTED):
        raise WithdrawalError(
            f"This request is already {withdrawal.get_status_display().lower()}."
        )
    withdrawal.status = status
    withdrawal.note = note or withdrawal.note
    withdrawal.processed_by = actor
    withdrawal.processed_at = timezone.now()
    withdrawal.save(update_fields=["status", "note", "processed_by", "processed_at"])
    return withdrawal
