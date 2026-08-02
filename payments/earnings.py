"""Earnings + withdrawable balance for experts, delivery leads and business devs.

A project's quote is split on approval: the assigned expert takes one share, the
delivery lead who quoted it takes another, the business developer who sourced it
takes a commission (only when one is attributed), and the remainder stays with
the platform. Shares are admin-editable in Site settings.

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
    if kind == Earning.Kind.EXPERT:
        return Decimal(cfg["expert_share_percent"])
    if kind == Earning.Kind.BUSINESS_DEV:
        return Decimal(cfg["business_dev_share_percent"])
    return Decimal(cfg["delivery_lead_share_percent"])


def project_share(project, kind):
    """(percent, amount) this project pays a role — honouring its overrides."""
    split = project.payout_split()
    if kind == Earning.Kind.EXPERT:
        return split["expert_percent"], split["expert_usd"]
    if kind == Earning.Kind.BUSINESS_DEV:
        return split["business_dev_percent"], split["business_dev_usd"]
    return split["delivery_lead_percent"], split["delivery_lead_usd"]


def share_amount(quote_usd, percent):
    return _money(Decimal(quote_usd or 0) * Decimal(percent) / Decimal(100))


def _earners(project):
    """(user, kind) pairs that a project pays out to, skipping unfilled roles."""
    pairs = []
    if project.expert_id:
        pairs.append((project.expert_id, Earning.Kind.EXPERT))
    if project.lead_id:
        pairs.append((project.lead_id, Earning.Kind.DELIVERY_LEAD))
    if project.business_developer_id:
        pairs.append((project.business_developer_id, Earning.Kind.BUSINESS_DEV))
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
        Q(expert=user) | Q(lead=user) | Q(business_developer=user),
        stage=Project.Stage.COMPLETED
    ).distinct()
    for project in completed:
        record_project_earnings(project)


def _projected(user):
    """Not-yet-earned value from the user's paid-but-unapproved projects."""
    active = Project.objects.filter(
        Q(expert=user) | Q(lead=user) | Q(business_developer=user),
        stage__in=PENDING_STAGES
    ).distinct()
    total = ZERO
    for project in active:
        split = project.payout_split()
        # One person can hold more than one role on a project; each counts.
        if project.expert_id == user.id:
            total += split["expert_usd"]
        if project.lead_id == user.id:
            total += split["delivery_lead_usd"]
        if project.business_developer_id == user.id:
            total += split["business_dev_usd"]
    return _money(total)


def _sum(queryset, field="amount_usd"):
    return _money(queryset.aggregate(total=Sum(field))["total"] or 0)


def has_custom_splits(user):
    """True when any of this user's projects is paid on its own percentages.

    The earnings screen quotes the site default; if some of a user's work pays
    differently, saying a flat "you earn X%" would be wrong.
    """
    return Project.objects.filter(
        Q(expert=user) | Q(lead=user) | Q(business_developer=user)
    ).filter(
        Q(expert_share_percent__isnull=False)
        | Q(delivery_lead_share_percent__isnull=False)
        | Q(business_dev_share_percent__isnull=False)
    ).exists()


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
        "expert_share_percent": Decimal(cfg["expert_share_percent"]),
        "delivery_lead_share_percent": Decimal(cfg["delivery_lead_share_percent"]),
        "business_dev_share_percent": Decimal(cfg["business_dev_share_percent"]),
    }


def available_balance(user):
    return summary(user, refresh=False)["available_usd"]


class WithdrawalError(Exception):
    """A withdrawal request that can't be honoured (amount, balance, details)."""


def _reference():
    return f"RIL-WD-{uuid.uuid4().hex[:10].upper()}"


@transaction.atomic
def request_withdrawal(user, amount_usd):
    """Validate a payout request against the live balance and record it.

    The destination is the earner's saved payout account, snapshotted onto the
    request — so what gets paid is what was approved, even if they edit their
    profile afterwards. The balance is re-read inside the transaction so two
    quick requests can't both clear the same funds.
    """
    from .paystack import charge_amount, usd_to_ngn_rate  # local import: avoids a cycle

    if not user.can_earn:
        raise WithdrawalError(
            "Only experts, delivery leads and business developers have earnings "
            "to withdraw."
        )
    if not user.has_payout_account:
        raise WithdrawalError(
            "Add your bank account in profile settings before withdrawing."
        )

    amount = _money(amount_usd)
    if amount <= ZERO:
        raise WithdrawalError("Enter an amount to withdraw.")

    minimum = _money(config()["min_withdrawal_usd"])
    if amount < minimum:
        raise WithdrawalError(f"The minimum withdrawal is ${minimum}.")

    available = summary(user)["available_usd"]
    if amount > available:
        raise WithdrawalError(f"You can withdraw up to ${available} right now.")

    currency, amount_subunit = charge_amount(amount)
    return Withdrawal.objects.create(
        user=user,
        reference=_reference(),
        amount_usd=amount,
        currency=currency,
        amount_subunit=amount_subunit,
        usd_to_ngn_rate=usd_to_ngn_rate() if currency == "NGN" else None,
        bank_name=user.bank_name,
        bank_code=user.bank_code,
        bank_account_number=user.bank_account_number,
        bank_account_name=user.bank_account_name,
        status=Withdrawal.Status.REQUESTED,
    )


def settle(withdrawal, status, actor, note="", manual=False):
    """Approve, reject, or record a payout on someone's behalf.

    Approving a payout **sends the money**: it creates the Paystack transfer and
    leaves the withdrawal "sending" until Paystack confirms it (webhook or a
    verify call). It is never marked paid just because we asked. Pass
    ``manual=True`` to record a payout made outside Paystack — the same escape
    hatch as before, and the only path when transfers are switched off.

    A delivery lead can't sign off their own payout — that needs a second pair of
    eyes. Superusers are exempt: they can already edit the row in the Django
    admin, so blocking them would be a control in name only (and a lone admin
    would have no way to ever be paid).
    """
    from . import transfers  # local import: keeps this module free of HTTP concerns
    from .paystack import PaystackError

    if withdrawal.user_id == actor.id and not actor.is_superuser:
        raise WithdrawalError(
            "You can't settle your own withdrawal — ask another delivery lead or an admin."
        )
    if withdrawal.status in Withdrawal.FINAL_STATUSES:
        raise WithdrawalError(
            f"This request is already {withdrawal.get_status_display().lower()}."
        )

    sending = (status == Withdrawal.Status.PAID and not manual
               and transfers.transfers_enabled())
    if sending:
        try:
            transfers.send(withdrawal, actor=actor)
        except PaystackError as exc:
            # Nothing moved — leave the request where it was so it can be retried.
            raise WithdrawalError(str(exc))
        if note:
            withdrawal.note = note
            withdrawal.save(update_fields=["note"])
        return withdrawal

    withdrawal.status = status
    withdrawal.note = note or withdrawal.note
    withdrawal.processed_by = actor
    withdrawal.processed_at = timezone.now()
    withdrawal.save(update_fields=["status", "note", "processed_by", "processed_at"])
    return withdrawal
