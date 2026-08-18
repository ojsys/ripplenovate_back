"""Refunds, and the reserve that pays for them.

The design in one sentence: **a refund never reaches into an expert's pocket.**

Money can be handed back from three places, tried in this order:

1. **What the project still holds** — the client paid the whole quote up front,
   so until everything has been approved and credited there is real cash sitting
   against that project which belongs to nobody yet. Refunding from here costs
   the platform its margin on that project and costs no one else anything.
2. **The reserve** — a slice of the platform's own share, set aside on every
   completed project precisely so that a project which failed *after* the team
   was paid can still be made right.
3. **The platform** — if the reserve is short, the shortfall is absorbed and
   recorded. Recorded rather than refused, because a business that cannot make a
   customer whole until an internal pot refills does not have a refund policy,
   it has an excuse.

What is deliberately absent: any path that produces a negative `Earning`. That
would make banked money conditional, and "the money is already in the building"
is the single strongest thing this platform offers the people who deliver work.
There is a test pinning it.
"""
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from accounts.models import SiteSettings
from projects.models import Project

from .models import Refund, ReserveEntry

logger = logging.getLogger("ripple")

ZERO = Decimal("0.00")


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# The reserve
# --------------------------------------------------------------------------

def reserve_balance():
    """What the reserve currently holds, derived from its own rows."""
    total = ZERO
    for entry in ReserveEntry.objects.all().only("kind", "amount_usd"):
        if entry.kind == ReserveEntry.Kind.CONTRIBUTION:
            total += entry.amount_usd
        else:
            total -= entry.amount_usd
    return _money(total)


def platform_share_usd(project):
    """What the platform kept on a completed project.

    The remainder, read the way the rest of the codebase reads it: collected
    money minus everything credited to a person. Not re-derived from current
    percentages, so an admin editing the shares tomorrow can't restate what was
    set aside today.
    """
    return _money(max(project.collected_usd - project.released_usd, ZERO))


@transaction.atomic
def contribute(project):
    """Earmark this project's slice of the platform share. Idempotent.

    Called when a project completes, and again lazily wherever earnings are
    backfilled — hence the unique constraint on the row and the `get_or_create`
    here. Contributing twice would inflate the reserve every time somebody
    opened their earnings page.
    """
    if project.stage != Project.Stage.COMPLETED:
        return None
    percent = SiteSettings.load().reserve_percent
    if percent <= 0:
        return None
    amount = _money(platform_share_usd(project) * percent / Decimal("100"))
    if amount <= ZERO:
        return None
    entry, created = ReserveEntry.objects.get_or_create(
        project=project,
        kind=ReserveEntry.Kind.CONTRIBUTION,
        defaults={
            "amount_usd": amount,
            "note": f"{percent}% of the platform share on {project.code}",
        },
    )
    return entry if created else None


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------

class RefundError(Exception):
    """A refund that can't be honoured — amount, state, or permission."""


def plan_funding(project, amount):
    """Split a proposed refund across held money, the reserve, and the platform.

    Pure arithmetic, no writes — so the UI can show a lead exactly what a
    refund will cost before they commit to it, and so the split can be tested
    without a database full of fixtures.
    """
    amount = _money(amount)
    from_held = min(amount, project.free_refund_usd)
    remainder = _money(amount - from_held)
    from_reserve = min(remainder, max(reserve_balance(), ZERO))
    absorbed = _money(remainder - from_reserve)
    return {
        "amount_usd": amount,
        "from_held_usd": _money(from_held),
        "from_reserve_usd": _money(from_reserve),
        "absorbed_usd": absorbed,
    }


def needs_admin(user, amount):
    """Whether this refund is above what a delivery lead may issue alone."""
    if user.is_superuser:
        return False
    return _money(amount) > SiteSettings.load().refund_admin_threshold_usd


@transaction.atomic
def request_refund(project, user, amount, reason):
    """Create a refund, approving it immediately where the caller may.

    Small goodwill refunds shouldn't need a second signature — making a lead
    wait on an admin to return $40 is how refunds stop being offered. Anything
    material does.
    """
    amount = _money(amount)
    reason = (reason or "").strip()
    if amount <= ZERO:
        raise RefundError("A refund has to be for more than zero.")
    if not reason:
        raise RefundError("Say why this is being refunded.")
    if not project.is_paid:
        raise RefundError("Nothing has been paid on this project yet.")
    if amount > project.refundable_usd:
        raise RefundError(
            f"That's more than is left to refund on this project "
            f"(${project.refundable_usd:,.2f} of ${project.collected_usd:,.2f} "
            "remaining)."
        )

    pending = needs_admin(user, amount)
    refund = Refund.objects.create(
        project=project,
        amount_usd=amount,
        reason=reason,
        requested_by=user,
        status=Refund.Status.REQUESTED if pending else Refund.Status.APPROVED,
        approved_by=None if pending else user,
    )
    return refund


@transaction.atomic
def approve_refund(refund, user):
    if refund.status != Refund.Status.REQUESTED:
        raise RefundError("That refund isn't waiting for a decision.")
    if not user.is_superuser:
        raise RefundError("Only an administrator can approve a refund this size.")
    refund.status = Refund.Status.APPROVED
    refund.approved_by = user
    refund.save(update_fields=["status", "approved_by"])
    return refund


@transaction.atomic
def reject_refund(refund, user, reason=""):
    if refund.status != Refund.Status.REQUESTED:
        raise RefundError("That refund isn't waiting for a decision.")
    refund.status = Refund.Status.REJECTED
    refund.approved_by = user
    refund.failure_reason = (reason or "").strip()[:255]
    refund.processed_at = timezone.now()
    refund.save(update_fields=["status", "approved_by", "failure_reason",
                               "processed_at"])
    return refund


@transaction.atomic
def settle(refund, *, manually=False, gateway="", gateway_reference="",
           gateway_raw=None):
    """Mark an approved refund as actually paid back, and fund it.

    The funding split is computed and written *here*, at the moment the money
    leaves, rather than when the refund was requested — the amount a project
    still holds moves as tasks are approved, so a split calculated at request
    time could be wrong by the time it settles.
    """
    if refund.status != Refund.Status.APPROVED:
        raise RefundError("Only an approved refund can be settled.")

    project = refund.project
    plan = plan_funding(project, refund.amount_usd)

    if plan["from_reserve_usd"] > ZERO:
        ReserveEntry.objects.create(
            kind=ReserveEntry.Kind.DRAW,
            amount_usd=plan["from_reserve_usd"],
            project=project,
            refund=refund,
            note=f"Refund on {project.code}",
        )

    refund.status = Refund.Status.PROCESSED
    refund.funded_from_held_usd = plan["from_held_usd"]
    refund.funded_from_reserve_usd = plan["from_reserve_usd"]
    refund.absorbed_usd = plan["absorbed_usd"]
    refund.settled_manually = manually
    refund.gateway = gateway
    refund.gateway_reference = gateway_reference
    refund.gateway_raw = gateway_raw or {}
    refund.processed_at = timezone.now()
    refund.save(update_fields=[
        "status", "funded_from_held_usd", "funded_from_reserve_usd",
        "absorbed_usd", "settled_manually", "gateway", "gateway_reference",
        "gateway_raw", "processed_at",
    ])
    if plan["absorbed_usd"] > ZERO:
        logger.warning(
            "Refund %s on %s exceeded the reserve by $%s — absorbed.",
            refund.id, project.code, plan["absorbed_usd"],
        )
    return refund


@transaction.atomic
def mark_failed(refund, reason):
    """The gateway refused or reversed it. The money never left."""
    refund.status = Refund.Status.FAILED
    refund.failure_reason = (reason or "")[:255]
    refund.processed_at = timezone.now()
    refund.save(update_fields=["status", "failure_reason", "processed_at"])
    return refund
