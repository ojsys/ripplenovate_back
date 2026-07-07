from django import template
from django.contrib.auth import get_user_model

from projects.models import Project

register = template.Library()
User = get_user_model()


@register.simple_tag
def ril_dashboard_stats():
    """Live numbers for the custom admin dashboard tiles."""
    Stage = Project.Stage
    qs = Project.objects.all()
    active = qs.exclude(stage=Stage.COMPLETED)
    contracted = sum(p.quote_usd for p in active)
    return {
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
