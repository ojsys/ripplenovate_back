"""What the platform itself has earned.

`reports.py` answers "how is each product line doing?" for whoever runs it. This
answers "how is the business doing?", and only for the people who run the
business.

Every figure comes from the **Earning ledger and the Payment record**, never
from re-deriving percentages — the same rule reporting already follows, for the
same reason: the ledger is what was actually paid, and a later change to a
percentage must not rewrite history.

Two kinds of money live here and they are deliberately kept apart:

* **Earned** (accrual) — the platform's share of work the client has signed off.
  A project's whole economics are attributed to the month it completed in, so
  the trend line is comparable month to month even though individual task
  payouts land whenever a lead approves them.
* **Cash** — what clients actually paid in, what earners have actually drawn
  out, and what is still owed to them. Mixing the two produces a number that
  means nothing, so they never share a chart.
"""
from collections import OrderedDict, defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Sum
from django.utils import timezone

from payments.models import Earning, Payment, Withdrawal

from .models import Project

User = get_user_model()
Stage = Project.Stage
ZERO = Decimal("0.00")

MONTHS_OF_HISTORY = 12


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _month_key(when):
    return f"{when.year:04d}-{when.month:02d}"


def _month_label(key):
    year, month = key.split("-")
    return f"{date(int(year), int(month), 1):%b}"


def _recent_months(count=MONTHS_OF_HISTORY):
    """The last `count` months, oldest first, including the current one."""
    today = timezone.localdate()
    months, year, month = [], today.year, today.month
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(months))


def _percent(part, whole):
    """A share, or None when the question doesn't apply.

    None rather than zero: no delivered work means the margin is unknown, not
    0%, and a chart that draws it as zero is telling a lie about a month that
    simply hasn't happened yet.
    """
    if not whole:
        return None
    return _money(Decimal(part) / Decimal(whole) * 100)


def platform_earnings():
    """Headline: what the platform has kept, and how that is trending."""
    delivered = Project.objects.filter(stage=Stage.COMPLETED)
    delivered_total = _money(delivered.aggregate(t=Sum("quote_usd"))["t"] or 0)
    credited = _money(
        Earning.objects.filter(project__in=delivered)
        .aggregate(t=Sum("amount_usd"))["t"] or 0
    )
    earned = _money(delivered_total - credited)

    # This month against last, on completion date.
    months = _recent_months(2)
    by_month = _monthly_platform()
    this_month = by_month.get(months[1], ZERO)
    last_month = by_month.get(months[0], ZERO)

    return {
        "earned_usd": str(earned),
        "delivered_value_usd": str(delivered_total),
        "team_cost_usd": str(credited),
        "margin_percent": (str(_percent(earned, delivered_total))
                           if delivered_total else None),
        "this_month_usd": str(this_month),
        "last_month_usd": str(last_month),
        # None when there's no prior month to compare against — "up 100%" from
        # nothing is not a fact about the business.
        "change_percent": (str(_percent(this_month - last_month, last_month))
                           if last_month else None),
        "delivered_count": delivered.count(),
    }


def _monthly_platform():
    """{month key: platform share} for every month that has delivered work."""
    delivered = (Project.objects
                 .filter(stage=Stage.COMPLETED, completed_at__isnull=False)
                 .prefetch_related("earnings"))
    out = defaultdict(lambda: ZERO)
    for project in delivered:
        cost = sum((e.amount_usd for e in project.earnings.all()), ZERO)
        out[_month_key(project.completed_at)] += _money(project.quote_usd) - cost
    return out


def monthly_trend():
    """Delivered value and the platform's share of it, month by month.

    Both are accrual figures on the same axis and the same scale, so they belong
    on one chart: delivered value is the context, the platform's share is the
    point.
    """
    delivered = (Project.objects
                 .filter(stage=Stage.COMPLETED, completed_at__isnull=False)
                 .prefetch_related("earnings"))

    totals = OrderedDict((key, {"delivered": ZERO, "platform": ZERO, "count": 0})
                         for key in _recent_months())
    for project in delivered:
        key = _month_key(project.completed_at)
        if key not in totals:
            continue
        cost = sum((e.amount_usd for e in project.earnings.all()), ZERO)
        row = totals[key]
        row["delivered"] += _money(project.quote_usd)
        row["platform"] += _money(project.quote_usd) - cost
        row["count"] += 1

    return [
        {
            "month": key,
            "label": _month_label(key),
            "delivered_usd": str(_money(row["delivered"])),
            "platform_usd": str(_money(row["platform"])),
            "delivered_count": row["count"],
        }
        for key, row in totals.items()
    ]


def cash_position():
    """Money in, money out, and what is still owed.

    Distinct from the earnings figures above: an expert can be credited today
    and withdraw in three weeks, and the platform holds the difference in the
    meantime. That balance is a liability, not revenue.
    """
    collected = _money(
        Payment.objects.filter(status=Payment.Status.SUCCESS)
        .aggregate(t=Sum("usd_total"))["t"] or 0
    )
    credited = _money(Earning.objects.aggregate(t=Sum("amount_usd"))["t"] or 0)
    withdrawn = _money(
        Withdrawal.objects.filter(status=Withdrawal.Status.PAID)
        .aggregate(t=Sum("amount_usd"))["t"] or 0
    )
    # Requested and processing are committed — the earner can't spend them
    # twice, so they count against what's still owed.
    in_flight_withdrawals = _money(
        Withdrawal.objects.filter(status__in=Withdrawal.OPEN_STATUSES)
        .aggregate(t=Sum("amount_usd"))["t"] or 0
    )
    # Credited against work the client hasn't signed off yet. Real money out on
    # projects that could still be disputed — the platform's live exposure.
    exposure = _money(
        Earning.objects.exclude(project__stage__in=Project.CLOSED_STAGES)
        .aggregate(t=Sum("amount_usd"))["t"] or 0
    )

    return {
        "collected_usd": str(collected),
        "credited_usd": str(credited),
        "paid_out_usd": str(withdrawn),
        "pending_withdrawals_usd": str(in_flight_withdrawals),
        "owed_to_team_usd": str(_money(credited - withdrawn - in_flight_withdrawals)),
        "exposure_usd": str(exposure),
    }


def where_it_goes():
    """How delivered value divides — the part-to-whole of a completed project.

    Sums the ledger rather than the configured percentages, so a project paid on
    an override, or an expert paid task by task, is counted as it was actually
    paid.
    """
    delivered = Project.objects.filter(stage=Stage.COMPLETED)
    total = _money(delivered.aggregate(t=Sum("quote_usd"))["t"] or 0)
    by_kind = {
        row["kind"]: _money(row["total"])
        for row in (Earning.objects.filter(project__in=delivered)
                    .values("kind").annotate(total=Sum("amount_usd")))
    }
    expert = by_kind.get(Earning.Kind.EXPERT, ZERO)
    lead = by_kind.get(Earning.Kind.DELIVERY_LEAD, ZERO)
    bizdev = by_kind.get(Earning.Kind.BUSINESS_DEV, ZERO)
    # The remainder, never a re-derived percentage, so the parts always close
    # on the whole exactly.
    platform = _money(total - expert - lead - bizdev)

    return {
        "total_usd": str(total),
        "parts": [
            {"key": "expert", "label": "Experts", "amount_usd": str(expert),
             "percent": _percent(expert, total) and str(_percent(expert, total))},
            {"key": "lead", "label": "Delivery leads", "amount_usd": str(lead),
             "percent": _percent(lead, total) and str(_percent(lead, total))},
            {"key": "bizdev", "label": "Business developers", "amount_usd": str(bizdev),
             "percent": _percent(bizdev, total) and str(_percent(bizdev, total))},
            {"key": "platform", "label": "Platform", "amount_usd": str(platform),
             "percent": _percent(platform, total) and str(_percent(platform, total))},
        ],
    }


def by_product_line():
    """Which disciplines actually make money, best margin first."""
    rows = {}
    for project in (Project.objects.filter(stage=Stage.COMPLETED)
                    .select_related("product_line")
                    .prefetch_related("earnings")):
        line = project.product_line
        key = line.slug if line else "unassigned"
        row = rows.setdefault(key, {
            "slug": key,
            "name": line.name if line else "No product line",
            "accent": line.accent if line else "#8B93A0",
            "delivered_usd": ZERO, "platform_usd": ZERO, "count": 0,
        })
        cost = sum((e.amount_usd for e in project.earnings.all()), ZERO)
        row["delivered_usd"] += _money(project.quote_usd)
        row["platform_usd"] += _money(project.quote_usd) - cost
        row["count"] += 1

    out = []
    for row in rows.values():
        margin = _percent(row["platform_usd"], row["delivered_usd"])
        out.append({
            **row,
            "delivered_usd": str(_money(row["delivered_usd"])),
            "platform_usd": str(_money(row["platform_usd"])),
            "margin_percent": str(margin) if margin is not None else None,
        })
    return sorted(out, key=lambda r: -float(r["platform_usd"]))


def pipeline():
    """What's in flight, by stage — the shape of what's coming."""
    counts = (Project.objects.exclude(stage__in=Project.CLOSED_STAGES)
              .values("stage")
              .annotate(n=Count("id"), value=Sum("quote_usd")))
    found = {row["stage"]: row for row in counts}
    return [
        {
            "stage": stage,
            "count": found.get(stage, {}).get("n", 0),
            "value_usd": str(_money(found.get(stage, {}).get("value") or 0)),
        }
        for stage in Project.STAGE_ORDER if stage != Stage.COMPLETED
    ]


def top_clients(limit=8):
    """Who the revenue actually comes from."""
    rows = (Project.objects.filter(stage=Stage.COMPLETED)
            .values("client_id", "client__full_name", "client__email", "company")
            .annotate(projects=Count("id"), delivered=Sum("quote_usd"))
            .order_by("-delivered")[:limit])
    return [
        {
            "id": row["client_id"],
            "name": row["client__full_name"] or row["client__email"],
            "company": row["company"] or "—",
            "projects": row["projects"],
            "delivered_usd": str(_money(row["delivered"])),
        }
        for row in rows
    ]


def dashboard():
    """Everything the analytics screen reads, in one call."""
    from . import reports

    # Reporting can't inherit the lazy backfill: an uncredited project has
    # value on the income side and nothing on the cost side, so it reads as
    # pure platform margin — wrong, and wrong in the flattering direction.
    reports.ensure_credited(Project.objects.all())

    return {
        "platform": platform_earnings(),
        "cash": cash_position(),
        "monthly": monthly_trend(),
        "split": where_it_goes(),
        "product_lines": by_product_line(),
        "pipeline": pipeline(),
        "top_clients": top_clients(),
        "generated_at": timezone.now(),
    }
