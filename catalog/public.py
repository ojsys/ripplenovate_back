"""What the public service pages are allowed to say.

Two rules shape this module, and both are about restraint:

**No people.** Not a name, not a photo, not a count of "our 47 designers". A
public directory of talent is the marketplace surface this platform
deliberately doesn't have — it puts experts in competition with each other and
hands a departing lead a portable reputation. The pages sell the *outcome*.

**No statistic drawn from too few projects.** "Delivered in an average of 18
days" across three briefs is not a fact about the platform, it's a fact about
three briefs, and one of those clients could recognise their own. Anything under
the threshold reports nothing rather than something shaky.
"""
from decimal import Decimal

from django.db.models import Avg, Count

# Below this, a line's numbers describe individual projects rather than the
# line, so they aren't published at all.
MIN_SAMPLE = 5


def line_stats(line):
    """Anonymised delivery record for one product line, or empty.

    Returns only what survives the sample threshold, so a caller can render
    whatever is present and never has to decide what's safe to show.
    """
    from projects.models import Project

    delivered = Project.objects.filter(
        product_line=line, stage=Project.Stage.COMPLETED,
        completed_at__isnull=False,
    )
    count = delivered.count()
    if count < MIN_SAMPLE:
        return {}

    cycle_days = [p.cycle_days for p in delivered if p.cycle_days is not None]
    punctual = [p.is_on_time for p in delivered if p.is_on_time is not None]

    stats = {"delivered_count": count}
    if len(cycle_days) >= MIN_SAMPLE:
        stats["avg_days"] = round(sum(cycle_days) / len(cycle_days))
    if len(punctual) >= MIN_SAMPLE:
        stats["on_time_percent"] = round(
            sum(1 for flag in punctual if flag) / len(punctual) * 100)
    return stats


def service_stats(service):
    """Same discipline, one service down."""
    from projects.models import Project

    delivered = Project.objects.filter(
        service=service, stage=Project.Stage.COMPLETED,
        completed_at__isnull=False,
    )
    count = delivered.count()
    if count < MIN_SAMPLE:
        return {}
    cycle_days = [p.cycle_days for p in delivered if p.cycle_days is not None]
    stats = {"delivered_count": count}
    if len(cycle_days) >= MIN_SAMPLE:
        stats["avg_days"] = round(sum(cycle_days) / len(cycle_days))
    return stats
