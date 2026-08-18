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

from django.db import transaction
from django.utils import timezone

from .models import CycleRun, Engagement, Project

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
    previous = engagement.cycles.order_by("-period_start").first()

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
    )

    # The team carries forward. A lead staffs the first cycle through the
    # ordinary assign flow and doesn't re-pick the same people every month.
    if previous:
        team = list(previous.experts.all())
        if team:
            cycle.experts.set(team)
            cycle.expert = previous.expert if previous.expert in team else team[0]
            cycle.save(update_fields=["expert"])

    return cycle


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
