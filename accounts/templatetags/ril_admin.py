from django import template
from django.contrib.auth import get_user_model

from projects.models import Project

register = template.Library()
User = get_user_model()


@register.filter
def startswith(value, arg):
    """{% if request.path|startswith:model.admin_url %} — for active nav state."""
    return bool(value) and str(value).startswith(str(arg))


@register.simple_tag
def ril_dashboard_stats():
    """Live numbers for the custom admin dashboard tiles."""
    Stage = Project.Stage
    qs = Project.objects.all()
    active = qs.exclude(stage=Stage.COMPLETED)
    contracted = sum(p.quote_usd for p in active)
    # What the platform keeps on delivered work: each quote minus the developer
    # and delivery-lead shares actually credited on approval.
    delivered = qs.filter(stage=Stage.COMPLETED).prefetch_related("earnings")
    platform = sum(p.payout_split()["platform_usd"] for p in delivered)
    return {
        "platform_fmt": "${:,.2f}".format(platform),
        "projects": qs.count(),
        "active": active.count(),
        "awaiting_quote": qs.filter(stage=Stage.SUBMITTED).count(),
        "ready_assign": qs.filter(stage=Stage.PAID, developer__isnull=True).count(),
        "in_review": qs.filter(stage=Stage.REVIEW).count(),
        "completed": qs.filter(stage=Stage.COMPLETED).count(),
        "contracted_fmt": "${:,}".format(contracted),
        "developers": User.objects.filter(role=User.Role.DEVELOPER).count(),
        "clients": User.objects.filter(role=User.Role.CLIENT).count(),
    }
