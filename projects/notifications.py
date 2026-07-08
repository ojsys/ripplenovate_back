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
            "Review the details and pay securely with Paystack to get the build started.",
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
            "A developer will be assigned and work begins shortly. Your funds are held securely and only "
            "released to the talent once you approve the delivered work.",
        ],
        cta=("Track your project", _project_url(project)),
    )
    send_brand_email(
        subject=f"Paid & ready to assign: {project.title}",
        to=_lead_emails(),
        heading="A project is paid and ready to assign",
        paragraphs=[f"“{project.title}” ({project.company}) has been paid. Assign a developer to kick it off."],
        cta=("Assign a developer", _project_url(project)),
    )


@_safe
def notify_developer_assigned(project):
    if project.developer:
        send_brand_email(
            subject=f"You've been assigned: {project.title}",
            to=project.developer.email,
            heading="You've got a new project",
            paragraphs=[
                f"Hi {project.developer.full_name or 'there'},",
                f"You've been assigned to build “{project.title}” for {project.company}.",
                "Open your task board to see the breakdown, check off tasks, and post progress updates.",
            ],
            cta=("Open my tasks", f"{_frontend()}/tasks"),
        )
    dev_name = project.developer.full_name if project.developer else "a developer"
    send_brand_email(
        subject=f"Work has started on {project.title}",
        to=project.client.email,
        heading="Your build is underway",
        paragraphs=[
            f"Good news — {dev_name} has been assigned to “{project.title}” and development has begun.",
            "You'll get updates as milestones are hit, and you can follow progress live any time.",
        ],
        cta=("Follow progress", _project_url(project)),
    )


@_safe
def notify_update_posted(project, activity):
    """Notify everyone involved (except the author) when a progress update is posted."""
    author_email = activity.author.email if activity.author else None
    recipients = {project.client.email}
    if project.developer:
        recipients.add(project.developer.email)
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
def notify_project_completed(project):
    recipients = set(_lead_emails())
    if project.developer:
        recipients.add(project.developer.email)
    send_brand_email(
        subject=f"Project completed: {project.title}",
        to=recipients,
        heading="A project was approved 🎉",
        paragraphs=[
            f"{_client_name(project)} approved delivery of “{project.title}”. Great work!",
        ],
        cta=("View the project", _project_url(project)),
    )
