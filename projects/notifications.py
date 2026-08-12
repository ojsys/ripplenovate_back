"""Project lifecycle email notifications.

Every function is wrapped so a notification failure is logged, never raised —
sending an email must never break the action that triggered it.
"""
import functools
import logging

from django.conf import settings
from django.contrib.auth import get_user_model

from ripple.mailer import send_brand_email

User = get_user_model()
logger = logging.getLogger("ripple")


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.error("notify %s failed: %s", fn.__name__, exc)

    return wrapper


def _frontend():
    return settings.FRONTEND_URL.rstrip("/")


def _project_url(project):
    return f"{_frontend()}/projects/{project.id}"


def _intake_lead_emails(project):
    """The leads who could actually pick this brief up.

    Was every delivery lead on the platform, which meant a lead in one
    discipline was emailed about work they can't see, can't quote, and has no
    stake in — and so was every lead still waiting to be approved. This mirrors
    the intake queue in `access.visible_projects`: an unclaimed brief belongs to
    the approved leads whose product lines cover it, and to nobody else.
    """
    if project.lead_id:
        # Somebody owns it; there is no queue to notify.
        return [project.lead.email] if project.lead else []
    if not project.product_line_id:
        return []
    candidates = User.objects.filter(
        role=User.Role.DELIVERY_LEAD,
        is_active=True,
        product_lines__id=project.product_line_id,
    ).distinct()
    return [lead.email for lead in candidates if lead.is_approved]


def _project_lead_email(project):
    """Just the lead running this project — a list so it composes with the rest."""
    return [project.lead.email] if project.lead_id and project.lead else []


def _team_emails(project):
    """Every expert delivering this project.

    Includes the primary expert whether or not the team membership was written
    — an admin can set one directly — so nobody working on a brief misses word
    that it changed.
    """
    emails = {e.email for e in project.experts.all()}
    if project.expert_id:
        emails.add(project.expert.email)
    return emails


def _money(n):
    return "${:,}".format(int(n or 0))


def _client_name(project):
    return project.client.full_name or project.client.email


@_safe
def notify_project_submitted(project):
    send_brand_email(
        subject=f"New project brief: {project.title}",
        to=_intake_lead_emails(project),
        heading="A new project brief was submitted",
        paragraphs=[
            f"{_client_name(project)} ({project.company or '—'}) submitted “{project.title}”.",
            f"Service: {project.category}. It's waiting for a quote.",
        ],
        cta=("Review & send a quote", _project_url(project)),
    )


@_safe
def notify_quote_sent(project):
    send_brand_email(
        subject=f"You've got a quote for {project.title}",
        to=project.client.email,
        heading=f"Your quote is ready — {_money(project.quote_usd)}",
        paragraphs=[
            f"Hi {project.client.full_name or 'there'},",
            f"We've scoped “{project.title}” and prepared a fixed quote of {_money(project.quote_usd)}.",
            "Review the details and pay securely with Paystack to get the work started.",
        ],
        cta=("Review quote & pay", _project_url(project)),
    )


@_safe
def notify_payment_received(project):
    send_brand_email(
        subject=f"Payment received for {project.title}",
        to=project.client.email,
        heading="Payment received — thank you!",
        paragraphs=[
            f"We've received your payment for “{project.title}”.",
            "An expert will be assigned and work begins shortly. Your funds are held securely and only "
            "released to the talent once you approve the delivered work.",
        ],
        cta=("Track your project", _project_url(project)),
    )
    send_brand_email(
        subject=f"Paid & ready to assign: {project.title}",
        to=_project_lead_email(project) or _intake_lead_emails(project),
        heading="A project is paid and ready to assign",
        paragraphs=[f"“{project.title}” ({project.company}) has been paid. Assign an expert to kick it off."],
        cta=("Assign an expert", _project_url(project)),
    )


@_safe
def notify_project_edited(project, summary, repriced=False):
    """Tell the client (and the assigned expert) that the lead changed something."""
    recipients = {project.client.email}
    recipients.update(_team_emails(project))
    paragraphs = [f"The delivery team updated “{project.title}”:", summary]
    if repriced:
        paragraphs.append(
            f"The quote is now {_money(project.quote_usd)}. Review the updated invoice "
            "before paying."
        )
    send_brand_email(
        subject=f"{project.title} was updated",
        to=recipients,
        heading="A project detail changed",
        paragraphs=paragraphs,
        cta=("View the project", _project_url(project)),
    )


def _names(people):
    """"Ada", or "Ada and Chidi", or "Ada, Chidi and Zainab"."""
    listed = [p.full_name or p.email for p in people]
    if not listed:
        return "an expert"
    if len(listed) == 1:
        return listed[0]
    return ", ".join(listed[:-1]) + f" and {listed[-1]}"


@_safe
def notify_experts_assigned(project, experts=None):
    """Tell each new expert they're on, and the client that work has started.

    Addressed one at a time rather than as a group: "You've been assigned" in a
    mail visibly sent to four people reads as somebody else's job.
    """
    experts = list(experts if experts is not None else project.experts.all())
    for expert in experts:
        send_brand_email(
            subject=f"You've been assigned: {project.title}",
            to=expert.email,
            heading="You've got a new project",
            paragraphs=[
                f"Hi {expert.full_name or 'there'},",
                f"You've been assigned to work on “{project.title}” for {project.company}.",
                "Open your task board to see the breakdown, check off tasks, and post progress updates.",
            ],
            cta=("Open my tasks", f"{_frontend()}/tasks"),
        )
    expert_name = _names(experts)
    send_brand_email(
        subject=f"Work has started on {project.title}",
        to=project.client.email,
        heading="Your project is underway",
        paragraphs=[
            f"Good news — {expert_name} {'have' if len(experts) > 1 else 'has'} been "
            f"assigned to “{project.title}” and work has begun.",
            "You'll get updates as milestones are hit, and you can follow progress live any time.",
        ],
        cta=("Follow progress", _project_url(project)),
    )


@_safe
def notify_update_posted(project, activity):
    """Notify everyone involved (except the author) when a progress update is posted."""
    author_email = activity.author.email if activity.author else None
    recipients = {project.client.email}
    recipients.update(_team_emails(project))
    recipients.update(_project_lead_email(project))
    recipients.discard(author_email)
    kind_label = activity.get_kind_display()
    send_brand_email(
        subject=f"New update on {project.title}",
        to=recipients,
        heading=f"New update · {kind_label}",
        paragraphs=[
            f"{activity.author_name} posted an update on “{project.title}”:",
            f"“{activity.text}”",
        ],
        cta=("View the project", _project_url(project)),
    )


@_safe
def notify_submitted_for_review(project):
    send_brand_email(
        subject=f"Ready for your review: {project.title}",
        to=project.client.email,
        heading="Your project is ready for review",
        paragraphs=[
            f"Hi {project.client.full_name or 'there'},",
            f"“{project.title}” has been completed and submitted for your review.",
            "Take a look and approve the delivery when you're happy — that's when funds are released to the talent.",
        ],
        cta=("Review & approve", _project_url(project)),
    )


@_safe
def notify_review_reminder(project):
    """A second nudge for a client sitting on a review."""
    send_brand_email(
        subject=f"Still waiting on your review: {project.title}",
        to=project.client.email,
        heading="Your project is ready for review",
        paragraphs=[
            f"Hi {project.client.full_name or 'there'},",
            f"“{project.title}” is finished and waiting for you to take a look.",
            "Approving the delivery closes the project and releases the funds to the "
            "team who built it.",
        ],
        cta=("Review & approve", _project_url(project)),
    )


@_safe
def notify_project_completed(project, completed_by_client=True):
    """Tell the delivery team the work is signed off.

    When a lead closes it out instead of the client, the client is told too — they
    should never find a project completed without hearing why.
    """
    recipients = set(_project_lead_email(project))
    recipients.update(_team_emails(project))
    who = (f"{_client_name(project)} approved delivery of"
           if completed_by_client else
           "The delivery team marked")
    send_brand_email(
        subject=f"Project completed: {project.title}",
        to=recipients,
        heading="A project was completed 🎉",
        paragraphs=[
            f"{who} “{project.title}”. Earnings have been credited to everyone who "
            "worked on it.",
        ],
        cta=("View the project", _project_url(project)),
    )
    if not completed_by_client:
        send_brand_email(
            subject=f"Project completed: {project.title}",
            to=project.client.email,
            heading="Your project has been completed",
            paragraphs=[
                f"Hi {project.client.full_name or 'there'},",
                f"“{project.title}” has been marked complete by the delivery team.",
                "If anything still needs attention, reply to this email and we'll pick "
                "it straight up.",
            ],
            cta=("View the project", _project_url(project)),
        )


def notify_attribution_changed(project, previous, current):
    """Tell a business developer they gained (or lost) credit for a project.

    Their commission depends on this, so neither change is silent.
    """
    if current:
        send_brand_email(
            subject=f"You're credited on {project.title}",
            to=current.email,
            heading="A project was credited to you",
            paragraphs=[
                f"Hi {current.full_name or 'there'},",
                f"“{project.title}” ({project.company}) is now attributed to you. "
                "Your commission is credited when the client approves delivery.",
            ],
            cta=("View the project", _project_url(project)),
        )
    if previous:
        send_brand_email(
            subject=f"Attribution changed on {project.title}",
            to=previous.email,
            heading="A project is no longer attributed to you",
            paragraphs=[
                f"Hi {previous.full_name or 'there'},",
                f"“{project.title}” ({project.company}) is no longer credited to you. "
                "If that looks wrong, reply to this email and we'll take a look.",
            ],
        )


def _money2(n):
    """Task amounts carry cents; project quotes never have."""
    return "${:,.2f}".format(n or 0)


@_safe
def notify_task_submitted(task):
    """Tell the lead running the project that work is waiting on them.

    Only the lead: approving is their call, and a task sitting unreviewed is
    money the expert can't reach yet.
    """
    project = task.project
    if not project.lead:
        return
    worth = (f" It's worth {_money2(task.amount_usd)} on approval."
             if task.amount_usd > 0 else "")
    send_brand_email(
        subject=f"Ready for review: {task.title}",
        to=project.lead.email,
        heading="A task was submitted for review",
        paragraphs=[
            f"{task.assignee.full_name or task.assignee.email} submitted "
            f"“{task.title}” on “{project.title}”.{worth}",
            "Approve it once you're happy with the work, or send it back with "
            "a note on what needs changing.",
        ],
        cta=("Review the task", _project_url(project)),
    )


@_safe
def notify_task_approved(task):
    """Tell the client their project moved, without naming anyone's fee.

    What an expert is paid is between them and the platform — it has no place
    in a client's inbox.
    """
    project = task.project
    send_brand_email(
        subject=f"Progress on {project.title}",
        to=project.client.email,
        heading="A piece of your project is done",
        paragraphs=[
            f"“{task.title}” has been completed and signed off on "
            f"“{project.title}”.",
            "You can follow the rest of the work live any time.",
        ],
        cta=("Follow progress", _project_url(project)),
    )


@_safe
def notify_task_changes_requested(task, note):
    project = task.project
    if not task.assignee:
        return
    send_brand_email(
        subject=f"Changes requested: {task.title}",
        to=task.assignee.email,
        heading="Your task needs another look",
        paragraphs=[
            f"Hi {task.assignee.full_name or 'there'},",
            f"The delivery lead sent “{task.title}” back on “{project.title}”:",
            f"“{note}”",
            "Make the changes and submit it again when you're ready.",
        ],
        cta=("Open the project", _project_url(project)),
    )
