"""Who can reach a project.

One definition of project scope, in two shapes: `visible_projects` for listing
and `can_access_project` for a single row. They are deliberately together —
endpoints that looked a project up by id and then authorised on
`role == DELIVERY_LEAD` alone handed every lead on the platform the whole
platform, because an admin carries the delivery_lead role too. The role says
what someone does, never which briefs are theirs.
"""
from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import Project

User = get_user_model()
Role = User.Role


def visible_projects(user):
    """The projects a delivery lead's board covers.

    Work they lead, plus briefs nobody has claimed yet in the disciplines they
    run. That second clause is the intake queue and nothing more: `lead` is
    unset until someone quotes, so without it a new brief would be invisible to
    every lead on the platform and could never be picked up at all.

    The moment a lead quotes a brief they own it, and it leaves everyone else's
    board — running the same discipline as someone is not a reason to see their
    client's budget.

    Mirrors ProjectViewSet.get_queryset so the stat tiles can never disagree
    with the table underneath them.
    """
    base = Project.objects.all()
    if user.is_superuser:
        return base
    lines = user.product_lines.values_list("id", flat=True)
    return base.filter(
        Q(lead=user) | (Q(lead__isnull=True) & Q(product_line__in=lines))
    ).distinct()


def can_access_project(user, project):
    """Whether this person is attached to this project at all.

    The client who commissioned it, the expert delivering it, the lead running
    it, the business developer credited with it, and admins — plus a lead
    looking at an unclaimed brief in one of their own disciplines, which is the
    only way one ever gets quoted. Nobody else: a brief can contain anything
    from budgets to unreleased plans.
    """
    if user.is_superuser:
        return True
    if user.id in (project.client_id, project.expert_id, project.lead_id,
                   project.business_developer_id):
        return True
    return (user.role == Role.DELIVERY_LEAD
            and project.lead_id is None
            and project.product_line_id is not None
            and user.product_lines.filter(id=project.product_line_id).exists())


def leads_project(user, project):
    """A delivery lead (or admin) who actually runs this brief.

    The write-side counterpart of `can_access_project`: acting on someone
    else's work — ticking off their tasks, removing their deliverables — takes
    both a claim on the project and an approved account. A lead whose
    application is still in review can watch their board but not touch it.
    """
    if not (user.is_superuser or user.role == Role.DELIVERY_LEAD):
        return False
    return user.is_approved and can_access_project(user, project)
