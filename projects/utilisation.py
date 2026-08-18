"""How much of an expert's available time actually earned money.

This is the number the whole talent proposition rests on and the platform could
not previously produce. An expert on Upwork keeps ~90% of what they bill and an
expert here keeps 60% — that comparison only survives if the 60% is billable far
more often, and until now nobody could say whether it was.

**The metric: billable coverage.** The share of days in a period on which the
expert held at least one project in In Progress or Review. Computed entirely
from timestamps the system already records — no time tracking, no timesheets,
no surveillance, none of which this platform wants to be in the business of.

It is deliberately a coverage measure rather than an hours measure. "Did you
have paid work on?" is answerable honestly from what we know; "how hard were
you working?" is not, and guessing at it would produce a confident number that
nobody should trust.

Three rules keep it honest:

* A day with three projects counts once. Coverage is about whether there was
  work, not how much.
* Someone who joined mid-period is measured against the part of the period they
  were actually here.
* An expert with no availability on record is excluded rather than assumed
  full-time — reported as unknown, with the sample size beside it.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from payments.models import Earning

from .models import Project

User = get_user_model()
ZERO = Decimal("0.00")

# Stages in which an expert is considered to have work on. Paid is excluded on
# purpose: the money has arrived but nobody has been asked to start yet, and
# counting it would credit the expert with days they spent waiting.
WORKING_STAGES = [Project.Stage.IN_PROGRESS, Project.Stage.REVIEW]


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _covered_days(project_spans, start, end):
    """Union of day-ranges, so overlapping projects don't double-count.

    Walking a set of dates rather than summing interval lengths — the periods
    here are months, not years, so the set is small and the arithmetic is
    obviously correct rather than cleverly correct.
    """
    days = set()
    for span_start, span_end in project_spans:
        cursor = max(span_start, start)
        last = min(span_end, end)
        while cursor <= last:
            days.add(cursor)
            cursor += timedelta(days=1)
    return len(days)


def _spans_for(projects, end):
    """When each project was live, as (first day, last day) date pairs.

    A project that is still running is open-ended, so it runs to the end of the
    window. `completed_at` is the honest close; a cancelled project stops when
    it was cancelled.
    """
    spans = []
    for project in projects:
        began = project.created_at.date()
        if project.completed_at:
            closed = project.completed_at.date()
        elif project.cancelled_at:
            closed = project.cancelled_at.date()
        else:
            closed = end
        spans.append((began, closed))
    return spans


def for_expert(user, start, end):
    """One expert's coverage and earnings across a window.

    Returns None for `coverage_percent` when the window contains no days this
    person was on the platform for — which is different from zero, and must not
    be shown as it.
    """
    joined = user.date_joined.date() if user.date_joined else start
    window_start = max(start, joined)
    if window_start > end:
        return {
            "user_id": user.id,
            "name": user.full_name or user.email,
            "available_days": 0,
            "covered_days": 0,
            "coverage_percent": None,
            "earned_usd": "0.00",
        }

    available = (end - window_start).days + 1
    projects = (
        Project.objects
        .filter(Q(experts=user) | Q(expert=user))
        .filter(stage__in=WORKING_STAGES + [Project.Stage.COMPLETED,
                                            Project.Stage.CANCELLED])
        .distinct()
    )
    # Only count a project's span if it actually reached a working stage. A
    # brief that went straight from Paid to Cancelled was never work.
    spans = _spans_for(
        [p for p in projects
         if p.stage in WORKING_STAGES or p.completed_at or p.cancelled_at],
        end,
    )
    covered = _covered_days(spans, window_start, end)

    earned = _money(sum(
        (e.amount_usd for e in Earning.objects.filter(
            user=user, created_at__date__gte=window_start,
            created_at__date__lte=end)),
        ZERO,
    ))
    return {
        "user_id": user.id,
        "name": user.full_name or user.email,
        "available_days": available,
        "covered_days": covered,
        "coverage_percent": round(covered / available * 100, 1) if available else None,
        "earned_usd": str(earned),
    }


def default_window(days=90):
    """The last quarter, which is long enough for a lull not to read as a trend."""
    end = timezone.localdate()
    return end - timedelta(days=days - 1), end


def for_roster(lead, start=None, end=None):
    """Every expert on one lead's roster, worst coverage first.

    Ordered that way deliberately: the useful thing on this screen is the
    person who has had nothing on for six weeks and is about to leave.
    """
    if start is None or end is None:
        start, end = default_window()
    rows = [for_expert(expert, start, end)
            for expert in lead.team_members.filter(role=User.Role.EXPERT)]
    rows.sort(key=lambda r: (r["coverage_percent"] is None, r["coverage_percent"] or 0))
    return {"start": str(start), "end": str(end), "experts": rows}


def platform(start=None, end=None):
    """The recruiting number: how busy the average expert actually is."""
    if start is None or end is None:
        start, end = default_window()
    rows = [for_expert(expert, start, end)
            for expert in User.objects.filter(role=User.Role.EXPERT, is_active=True)]
    measured = [r["coverage_percent"] for r in rows
                if r["coverage_percent"] is not None]
    return {
        "start": str(start),
        "end": str(end),
        # Null rather than 0 when there's nobody to measure — the difference
        # between "our experts are idle" and "we have no experts yet".
        "avg_coverage_percent": (
            round(sum(measured) / len(measured), 1) if measured else None
        ),
        "sample": len(measured),
        "expert_count": len(rows),
        "fully_idle": sum(1 for r in rows if r["coverage_percent"] == 0),
    }
