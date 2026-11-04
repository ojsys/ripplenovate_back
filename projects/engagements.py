"""Retainers: ongoing work billed monthly.

Virtual assistance, bookkeeping, customer support and social media management
are not projects. They are seats — the same work every month, for as long as
somebody wants it. The platform launched all four as product lines while the
money model could only express a one-shot brief, so a client wanting a VA for
six months had to post six briefs.

**The design decision that makes this small: a cycle is a Project.**

Rather than a parallel entity with its own tasks, activity, attachments,
payments and payouts, each month materialises an ordinary `Project` pointing at
its `Engagement`. Everything downstream — the six-stage lifecycle, per-task
payouts, the earnings ledger, refunds, reporting, the delivery board — works
without knowing engagements exist. A standalone brief simply has a null
`engagement`, which is every project written before today.

Generation is the one thing in this codebase that creates billable records with
no human in the loop, so it is deliberately timid: a dry-run mode, a hard cap
per run, a written log of every pass, and a refusal to run ahead of an unpaid
cycle.
"""
import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import CycleRun, Engagement, Project, Task

logger = logging.getLogger("ripple")

# How far ahead of a period a cycle is raised, so the client has time to pay
# before the month it covers actually starts.
LEAD_DAYS = 7

# Nothing legitimate creates more than a handful of cycles in one pass. A run
# that wants more than this has almost certainly been handed a bad date, and
# stopping is far better than billing forty clients twice.
MAX_PER_RUN = 50


def _clamp_day(year, month, day):
    """A billing day that exists in this month.

    Billing days are capped at 28 on the way in, so this only bites for legacy
    rows — but a retainer that silently skips February is worse than one that
    bills on the 28th.
    """
    return min(day, calendar.monthrange(year, month)[1])


def next_period_start(engagement, after=None):
    """The first day of the next unbilled month for this engagement.

    Driven off the cycles that exist rather than a stored cursor: the rows are
    the record, and a cursor is a second source of truth that can drift from
    them.
    """
    last = (engagement.cycles.order_by("-period_start")
            .values_list("period_start", flat=True).first())
    if last:
        year, month = last.year, last.month
        month += 1
        if month > 12:
            year, month = year + 1, 1
        return date(year, month, _clamp_day(year, month, engagement.billing_day))

    start = after or engagement.started_on or timezone.localdate()
    day = _clamp_day(start.year, start.month, engagement.billing_day)
    first = date(start.year, start.month, day)
    if first < start:
        # The billing day has already gone this month; start next month rather
        # than raising a cycle for a period that's half over.
        year, month = (start.year, start.month + 1)
        if month > 12:
            year, month = year + 1, 1
        first = date(year, month, _clamp_day(year, month, engagement.billing_day))
    return first


def period_end_for(engagement, period_start):
    """The last day covered — the day before the next billing day."""
    year, month = period_start.year, period_start.month + 1
    if month > 12:
        year, month = year + 1, 1
    nxt = date(year, month, _clamp_day(year, month, engagement.billing_day))
    return nxt - timedelta(days=1)


def blocking_cycle(engagement, on=None):
    """An unpaid cycle that should stop the next one being raised.

    This is the credit control, and the reason net-30 invoicing was deferred:
    a client who hasn't paid for June does not get July as well. Only a cycle
    whose period has actually started counts — one raised a week early and not
    yet due is not a debt.

    `on` is threaded through rather than read from the clock. It read
    `timezone.localdate()` at first, which was right under a real cron and
    silently wrong under `--on`: checking a future date would compare next
    month's cycles against today and find nothing overdue, so a dry run for
    October would happily report raising it while September sat unpaid.
    """
    today = on or timezone.localdate()
    return (engagement.cycles
            .filter(stage=Project.Stage.QUOTED, period_start__lte=today)
            .order_by("period_start").first())


def due_for_generation(engagement, on=None):
    """Whether this engagement should have a cycle raised today, and why not."""
    today = on or timezone.localdate()
    if engagement.status != Engagement.Status.ACTIVE:
        return False, f"{engagement.get_status_display().lower()}"
    if engagement.ends_on and engagement.ends_on < today:
        return False, "past its end date"
    blocked = blocking_cycle(engagement, on=today)
    if blocked:
        return False, f"unpaid cycle from {blocked.period_start}"

    period_start = next_period_start(engagement)
    if engagement.ends_on and period_start > engagement.ends_on:
        return False, "next period falls after the end date"
    if period_start - timedelta(days=LEAD_DAYS) > today:
        return False, f"not due until {period_start - timedelta(days=LEAD_DAYS)}"
    return True, ""


@transaction.atomic
def generate_cycle(engagement, period_start=None):
    """Materialise one month as an ordinary project. Idempotent.

    Keyed on (engagement, period_start), so a double run is a no-op rather than
    a double bill. Returns the project, or None if that period already exists.
    """
    period_start = period_start or next_period_start(engagement)
    if engagement.cycles.filter(period_start=period_start).exists():
        return None

    period_end = period_end_for(engagement, period_start)
    # The last month that actually ran. A month cancelled unpaid (the retainer
    # was paused and its pending invoice withdrawn, say) says nothing about who
    # does the work or what it's made of, so it's skipped as a template.
    previous = (engagement.cycles.exclude(stage=Project.Stage.CANCELLED)
                .order_by("-period_start").first())

    cycle = Project.objects.create(
        engagement=engagement,
        period_start=period_start,
        period_end=period_end,
        title=f"{engagement.title} · {period_start:%B %Y}",
        client=engagement.client,
        organisation=engagement.organisation,
        company=engagement.organisation.name if engagement.organisation else "",
        product_line=engagement.product_line,
        service=engagement.service,
        category=(engagement.service.name if engagement.service
                  else engagement.title),
        description=engagement.description,
        lead=engagement.lead,
        # Straight to Quoted: the price was agreed when the retainer was set
        # up, so there is nothing to quote each month — only to pay.
        stage=Project.Stage.QUOTED,
        quote_usd=int(engagement.monthly_amount_usd),
        target_date=period_end,
        # Money terms carry forward exactly as they were agreed. Without this
        # every month after the first silently reverted to site defaults, and
        # the business developer who brought the client in — credited on every
        # one-off brief through the same referral — earned nothing on a
        # retainer at all.
        business_developer=(previous.business_developer if previous
                            else engagement.client.referred_by),
        expert_share_percent=previous.expert_share_percent if previous else None,
        delivery_lead_share_percent=(previous.delivery_lead_share_percent
                                     if previous else None),
        business_dev_share_percent=(previous.business_dev_share_percent
                                    if previous else None),
    )

    # The team carries forward. A lead staffs the first cycle through the
    # ordinary assign flow and doesn't re-pick the same people every month.
    if previous:
        team = list(previous.experts.all())
        if team:
            cycle.experts.set(team)
            cycle.expert = previous.expert if previous.expert in team else team[0]
            cycle.save(update_fields=["expert"])
        _carry_tasks(previous, cycle, team)

    return cycle


def _carry_tasks(previous, cycle, team):
    """Copy last month's task list into this one, as fresh to-dos.

    A retainer is the same work every month; making the lead retype the list
    each time is how a month ends up with nothing on it and nobody able to see
    what's been done. Status never carries — every task starts at To do, so no
    approval (and no money) leaks from one month into the next.

    Prices carry only if they still fit this month's expert pool. Last month
    may have had paid extra scope that this one doesn't, and the allocation
    invariant — never promise out more than was taken in — is not something a
    scheduled job gets to bend. If they don't fit, the list carries unpriced
    and the lead re-prices it.
    """
    tasks = list(previous.tasks.order_by("order", "id"))
    if not tasks:
        return
    on_team = {e.id for e in team}
    total = sum((t.amount_usd for t in tasks), Decimal("0"))
    keep_prices = total <= cycle.expert_pool_usd
    Task.objects.bulk_create([
        Task(
            project=cycle,
            title=t.title,
            description=t.description,
            assignee_id=t.assignee_id if t.assignee_id in on_team else None,
            amount_usd=t.amount_usd if keep_prices else Decimal("0"),
            order=t.order,
        )
        for t in tasks
    ])


def start_if_staffed(cycle, actor=None):
    """Kick a freshly paid cycle straight into delivery if it has a team.

    A brief waits at Paid for a lead to staff it. A retainer month usually
    doesn't need to — its team carried forward from last month — but nothing
    moved it on, and the whole platform reads Paid as "nobody has started":
    experts' My Work skipped it, progress ignored its tasks, and the board
    offered the lead no action because an expert was already named. Each month
    sat stalled until somebody noticed.

    Returns True if it started the cycle.
    """
    from . import notifications
    from .views import log_activity

    if (not cycle.engagement_id or cycle.stage != Project.Stage.PAID
            or not cycle.expert_id):
        return False
    cycle.stage = Project.Stage.IN_PROGRESS
    cycle.save(update_fields=["stage"])
    team = list(cycle.experts.all())
    author = actor or cycle.lead or cycle.client
    log_activity(cycle, author,
                 f"{cycle.period_start:%B}'s team carried forward — delivery has started.")
    try:
        notifications.notify_experts_assigned(cycle, team)
    except Exception as exc:  # noqa: BLE001 — payment must not fail on email
        logger.error("kickoff notification for %s failed: %s", cycle.code, exc)
    return True


def withdraw_unpaid_cycles(engagement, actor, reason):
    """Cancel every month that was raised but never paid.

    Run when a retainer ends. Cycles are raised a week ahead, so ending one
    almost always leaves next month's invoice sitting open — still payable,
    for a retainer that no longer exists. Nothing has been done on an unpaid
    cycle (work can't start before payment), so there is nothing to refund
    and nothing to deliver.
    """
    from .views import log_activity

    withdrawn = []
    for cycle in engagement.cycles.filter(stage=Project.Stage.QUOTED):
        cycle.stage = Project.Stage.CANCELLED
        cycle.cancelled_at = timezone.now()
        cycle.cancelled_by = actor
        cycle.cancellation_reason = f"Retainer ended: {reason}"
        cycle.save(update_fields=["stage", "cancelled_at", "cancelled_by",
                                  "cancellation_reason"])
        log_activity(cycle, actor,
                     "Withdrew this unpaid month — the retainer has ended.")
        withdrawn.append(cycle)
    return withdrawn


def run(*, dry_run=False, on=None, limit=MAX_PER_RUN, triggered_by=""):
    """One pass over every engagement. The scheduled job's whole body.

    Writes a `CycleRun` either way, including dry runs — "did the cron fire?"
    is a question somebody will ask at 2am, and an empty log is a much clearer
    answer than an absent one.
    """
    today = on or timezone.localdate()
    created, skipped, notes = [], 0, []

    for engagement in Engagement.objects.select_related(
        "organisation", "client", "lead", "product_line", "service"
    ).order_by("id"):
        due, why = due_for_generation(engagement, today)
        if not due:
            skipped += 1
            if why and "not due until" not in why:
                notes.append(f"{engagement.id}: {why}")
            continue
        if len(created) >= limit:
            notes.append(
                f"stopped at the {limit}-cycle cap — {engagement.id} and any "
                "after it were not generated"
            )
            break
        if dry_run:
            created.append(
                f"would raise {engagement.title} for "
                f"{next_period_start(engagement)}")
            continue
        cycle = generate_cycle(engagement)
        if cycle:
            created.append(f"{cycle.code} · {cycle.title}")
            _announce(cycle)

    entry = CycleRun.objects.create(
        dry_run=dry_run,
        created_count=len(created),
        skipped_count=skipped,
        detail="\n".join(created + notes)[:4000],
        triggered_by=triggered_by[:100],
    )
    logger.info("Cycle run %s: %s created, %s skipped%s",
                entry.id, len(created), skipped, " (dry run)" if dry_run else "")
    return entry, created


def _announce(cycle):
    from . import notifications

    try:
        notifications.notify_cycle_raised(cycle)
    except Exception as exc:  # noqa: BLE001 — billing must not fail on email
        logger.error("cycle notification for %s failed: %s", cycle.code, exc)
