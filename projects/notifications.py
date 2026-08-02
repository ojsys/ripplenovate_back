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


def _lead_emails():
    return list(
        User.objects.filter(role=User.Role.DELIVERY_LEAD).values_list("email", flat=True)
    )


def _money(n):
    return "${:,}".format(int(n or 0))


def _client_name(project):
    return project.client.full_name or project.client.email


@_safe
def notify_project_submitted(project):
    send_brand_email(
        subject=f"New project brief: {project.title}",
        to=_lead_emails(),
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
        to=_lead_emails(),
        heading="A project is paid and ready to assign",
        paragraphs=[f"“{project.title}” ({project.company}) has been paid. Assign an expert to kick it off."],
        cta=("Assign an expert", _project_url(project)),
    )


@_safe
def notify_project_edited(project, summary, repriced=False):
    """Tell the client (and the assigned expert) that the lead changed something."""
    recipients = {project.client.email}
    if project.expert:
        recipients.add(project.expert.email)
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


@_safe
def notify_expert_assigned(project):
    if project.expert:
        send_brand_email(
            subject=f"You've been assigned: {project.title}",
            to=project.expert.email,
            heading="You've got a new project",
            paragraphs=[
                f"Hi {project.expert.full_name or 'there'},",
                f"You've been assigned to build “{project.title}” for {project.company}.",
                "Open your task board to see the breakdown, check off tasks, and post progress updates.",
            ],
            cta=("Open my tasks", f"{_frontend()}/tasks"),
        )
    expert_name = project.expert.full_name if project.expert else "an expert"
    send_brand_email(
        subject=f"Work has started on {project.title}",
        to=project.client.email,
        heading="Your project is underway",
        paragraphs=[
            f"Good news — {expert_name} has been assigned to “{project.title}” and work has begun.",
            "You'll get updates as milestones are hit, and you can follow progress live any time.",
        ],
        cta=("Follow progress", _project_url(project)),
    )


@_safe
def notify_update_posted(project, activity):
    """Notify everyone involved (except the author) when a progress update is posted."""
    author_email = activity.author.email if activity.author else None
    recipients = {project.client.email}
    if project.expert:
        recipients.add(project.expert.email)
    recipients.update(_lead_emails())
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
    recipients = set(_lead_emails())
    if project.expert:
        recipients.add(project.expert.email)
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
