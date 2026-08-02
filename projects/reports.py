"""Reporting across product lines, business developers and delivery leads.

Every money figure here comes from the **Earning ledger**, not from
recalculating percentages. Earnings are snapshotted when a client approves
delivery, so the ledger is what was actually paid; re-deriving the split at
report time would quietly disagree with it the moment a percentage changed.

The platform's share is likewise the remainder in aggregate — delivered value
minus what was credited out — which is the same invariant `payout_split()`
enforces per project, so the two can never tell different stories.
"""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum

from payments.models import Earning

from .models import Project

User = get_user_model()
Stage = Project.Stage
ZERO = Decimal("0.00")


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _avg(values):
    return round(sum(values) / len(values), 1) if values else None


def _rate(flags):
    """Percentage of True in a list of booleans, or None if the list is empty.

    `is_on_time` returns None for projects the question doesn't apply to — no
    target agreed, or not delivered — and those are filtered out before they get
    here. A project that never promised a date can't have missed one, so
    counting it as a miss would understate every team.
    """
    if not flags:
        return None
    return round(sum(1 for f in flags if f) / len(flags) * 100, 1)


def ensure_credited(projects):
    """Credit any delivered project whose earnings were never written.

    Crediting on approval is deliberately best-effort — `credit_earnings()`
    swallows failures so a payout bug can never block a client's approval — and
    the system relies on a lazy backfill the next time that earner opens their
    earnings page.

    Reporting cannot inherit that laziness. An uncredited delivered project has
    value on the income side and nothing on the cost side, so it reads as **pure
    platform margin** — the number would be wrong, and wrong in the flattering
    direction, until somebody happened to open the right page. Recording is
    idempotent, so this just closes the gap.
    """
    from payments import earnings as earnings_service

    uncredited = (
        projects
        .filter(stage=Stage.COMPLETED, earnings__isnull=True)
        .exclude(quote_usd=0)
        .distinct()
    )
    for project in uncredited:
        earnings_service.record_project_earnings(project)


def product_lines(scope=None):
    """Per-discipline P&L: what came in, what went out, what the platform kept."""
    projects = scope if scope is not None else Project.objects.all()
    ensure_credited(projects)
    projects = projects.select_related("product_line")

    rows = {}

    def row_for(line):
        key = line.slug if line else "unassigned"
        if key not in rows:
            rows[key] = {
                "slug": key,
                "name": line.name if line else "No product line",
                "accent": line.accent if line else "#8B93A0",
                "active_count": 0, "active_value_usd": 0,
                "delivered_count": 0, "delivered_value_usd": 0,
                "expert_cost_usd": ZERO, "lead_cost_usd": ZERO,
                "bizdev_cost_usd": ZERO, "platform_usd": ZERO,
                "cycle_days": [], "on_time": [],
                "leads": 0, "experts": 0,
            }
        return rows[key]

    for project in projects:
        row = row_for(project.product_line)
        if project.stage == Stage.COMPLETED:
            row["delivered_count"] += 1
            row["delivered_value_usd"] += project.quote_usd
            if project.cycle_days is not None:
                row["cycle_days"].append(project.cycle_days)
            if project.is_on_time is not None:
                row["on_time"].append(project.is_on_time)
        else:
            row["active_count"] += 1
            row["active_value_usd"] += project.quote_usd

    # One grouped query for the whole ledger rather than a split per project.
    credited = (
        Earning.objects
        .filter(project__in=projects)
        .values("project__product_line__slug", "kind")
        .annotate(total=Sum("amount_usd"))
    )
    cost_field = {
        Earning.Kind.EXPERT: "expert_cost_usd",
        Earning.Kind.DELIVERY_LEAD: "lead_cost_usd",
        Earning.Kind.BUSINESS_DEV: "bizdev_cost_usd",
    }
    for entry in credited:
        key = entry["project__product_line__slug"] or "unassigned"
        if key in rows:
            rows[key][cost_field[entry["kind"]]] += _money(entry["total"])

    # Headcount per line, in two queries rather than one per row.
    members = (
        User.objects
        .filter(product_lines__isnull=False)
        .values("product_lines__slug", "role")
        .annotate(n=Count("id"))
    )
    for entry in members:
        key = entry["product_lines__slug"]
        if key in rows:
            if entry["role"] == User.Role.DELIVERY_LEAD:
                rows[key]["leads"] += entry["n"]
            elif entry["role"] == User.Role.EXPERT:
                rows[key]["experts"] += entry["n"]

    out = []
    for row in rows.values():
        delivered = _money(row["delivered_value_usd"])
        paid_out = row["expert_cost_usd"] + row["lead_cost_usd"] + row["bizdev_cost_usd"]
        # The remainder, exactly as in payout_split() — never a re-derived percentage.
        row["platform_usd"] = _money(delivered - paid_out)
        row["avg_cycle_days"] = _avg(row["cycle_days"])
        row["on_time_percent"] = _rate(row["on_time"])
        row["on_time_sample"] = len(row["on_time"])
        row["margin_percent"] = (
            _money(row["platform_usd"] / delivered * 100) if delivered else None
        )
        row.pop("cycle_days")
        row.pop("on_time")
        for key in ("expert_cost_usd", "lead_cost_usd", "bizdev_cost_usd", "platform_usd"):
            row[key] = str(row[key])
        row["margin_percent"] = str(row["margin_percent"]) if row["margin_percent"] is not None else None
        out.append(row)

    # Busiest first: delivered value, then what's still in flight.
    return sorted(out, key=lambda r: (-float(r["delivered_value_usd"]), -r["active_value_usd"]))


def business_developers():
    """Leaderboard: who is sourcing work, and what it has earned them."""
    people = (
        User.objects
        .filter(role=User.Role.BUSINESS_DEV)
        .annotate(
            clients=Count("referred_clients", distinct=True),
            sourced=Count("sourced_projects", distinct=True),
        )
    )

    sourced_totals = (
        Project.objects
        .filter(business_developer__isnull=False)
        .values("business_developer_id")
        .annotate(
            total=Sum("quote_usd"),
            won=Sum("quote_usd", filter=Q(stage=Stage.COMPLETED)),
            won_count=Count("id", filter=Q(stage=Stage.COMPLETED)),
        )
    )
    by_user = {row["business_developer_id"]: row for row in sourced_totals}

    earned = (
        Earning.objects
        .filter(kind=Earning.Kind.BUSINESS_DEV)
        .values("user_id")
        .annotate(total=Sum("amount_usd"))
    )
    earned_by_user = {row["user_id"]: _money(row["total"]) for row in earned}

    rows = []
    for person in people:
        totals = by_user.get(person.id, {})
        sourced_value = totals.get("total") or 0
        won_value = totals.get("won") or 0
        won_count = totals.get("won_count") or 0
        commission = earned_by_user.get(person.id, ZERO)
        rows.append({
            "id": person.id,
            "name": person.full_name or person.email,
            "email": person.email,
            "clients_referred": person.clients,
            "projects_sourced": person.sourced,
            "projects_won": won_count,
            "sourced_value_usd": sourced_value,
            "won_value_usd": won_value,
            "commission_earned_usd": str(commission),
            # Share of what they sourced that has actually been delivered. Only
            # meaningful once they've sourced something, so it's null until then.
            "conversion_percent": (
                str(_money(Decimal(won_count) / Decimal(person.sourced) * 100))
                if person.sourced else None
            ),
        })
    return sorted(rows, key=lambda r: -float(r["commission_earned_usd"]))


def delivery_leads():
    """Scorecard: team size, what each lead has delivered, what's in flight."""
    leads = (
        User.objects
        .filter(role=User.Role.DELIVERY_LEAD)
        .prefetch_related("product_lines")
        .annotate(team_size=Count("team_members", distinct=True))
    )

    led = (
        Project.objects
        .filter(lead__isnull=False)
        .values("lead_id")
        .annotate(
            projects=Count("id"),
            delivered=Count("id", filter=Q(stage=Stage.COMPLETED)),
            delivered_value=Sum("quote_usd", filter=Q(stage=Stage.COMPLETED)),
            in_flight_value=Sum("quote_usd", filter=~Q(stage=Stage.COMPLETED)),
        )
    )
    by_lead = {row["lead_id"]: row for row in led}

    earned = (
        Earning.objects
        .filter(kind=Earning.Kind.DELIVERY_LEAD)
        .values("user_id")
        .annotate(total=Sum("amount_usd"))
    )
    earned_by_lead = {row["user_id"]: _money(row["total"]) for row in earned}

    cycles = defaultdict(list)
    on_time = defaultdict(list)
    for project in Project.objects.filter(
        stage=Stage.COMPLETED, lead__isnull=False, completed_at__isnull=False
    ):
        cycles[project.lead_id].append(project.cycle_days)
        if project.is_on_time is not None:
            on_time[project.lead_id].append(project.is_on_time)

    # Work that's already past its promised date and still running — the number a
    # lead can actually do something about today.
    overdue = defaultdict(int)
    for project in Project.objects.filter(
        lead__isnull=False, target_date__isnull=False
    ).exclude(stage=Stage.COMPLETED):
        if project.is_overdue:
            overdue[project.lead_id] += 1

    rows = []
    for lead in leads:
        totals = by_lead.get(lead.id, {})
        rows.append({
            "id": lead.id,
            "name": lead.full_name or lead.email,
            "email": lead.email,
            "product_lines": [
                {"slug": line.slug, "name": line.name, "accent": line.accent}
                for line in lead.product_lines.all()
            ],
            "team_size": lead.team_size,
            "projects_led": totals.get("projects") or 0,
            "projects_delivered": totals.get("delivered") or 0,
            "delivered_value_usd": totals.get("delivered_value") or 0,
            "in_flight_value_usd": totals.get("in_flight_value") or 0,
            "earned_usd": str(earned_by_lead.get(lead.id, ZERO)),
            "avg_cycle_days": _avg(cycles.get(lead.id, [])),
            "on_time_percent": _rate(on_time.get(lead.id, [])),
            "on_time_sample": len(on_time.get(lead.id, [])),
            "overdue_count": overdue.get(lead.id, 0),
        })
    return sorted(rows, key=lambda r: -r["delivered_value_usd"])


def totals(scope=None):
    """Platform-wide headline figures, consistent with the per-line rows."""
    projects = scope if scope is not None else Project.objects.all()
    ensure_credited(projects)
    delivered = projects.filter(stage=Stage.COMPLETED)
    active = projects.exclude(stage=Stage.COMPLETED)

    delivered_value = _money(delivered.aggregate(t=Sum("quote_usd"))["t"] or 0)
    paid_out = _money(
        Earning.objects.filter(project__in=projects)
        .aggregate(t=Sum("amount_usd"))["t"] or 0
    )
    cycle = [p.cycle_days for p in delivered if p.cycle_days is not None]
    punctuality = [p.is_on_time for p in delivered if p.is_on_time is not None]
    overdue = sum(1 for p in active if p.is_overdue)

    return {
        "delivered_count": delivered.count(),
        "delivered_value_usd": str(delivered_value),
        "active_count": active.count(),
        "active_value_usd": str(_money(active.aggregate(t=Sum("quote_usd"))["t"] or 0)),
        "paid_out_usd": str(paid_out),
        "platform_usd": str(_money(delivered_value - paid_out)),
        "margin_percent": (
            str(_money((delivered_value - paid_out) / delivered_value * 100))
            if delivered_value else None
        ),
        "avg_cycle_days": _avg(cycle),
        # How many delivered projects we can actually date. Reporting on a
        # partial sample without saying so would overstate its authority.
        "cycle_sample": len(cycle),
        "on_time_percent": _rate(punctuality),
        "on_time_sample": len(punctuality),
        "overdue_count": overdue,
    }
