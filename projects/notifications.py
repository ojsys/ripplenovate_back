"""Project lifecycle email notifications.

Every function is wrapped so a notification failure is logged, never raised —
sending an email must never break the action that triggered it.
"""
import functools
import logging

from django.conf import settings
from django.contrib.auth import get_user_model

from ripple.mailer import send_brand_email

from .models import Project

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
    """Tell people an update was posted — but only the right people.

    A top-level update is news for everyone on the project. A **reply** is not:
    it belongs to a conversation, and mailing the whole team every time two
    people go back and forth about one file is how a feed people read becomes a
    filter rule they don't. Replies go to the thread — whoever started it and
    whoever has answered — plus the project's lead, who is accountable for the
    work whether or not they've spoken yet.
    """
    author_email = activity.author.email if activity.author else None
    is_reply = activity.parent_id is not None

    if is_reply:
        recipients = set(activity.thread_emails) | set(_project_lead_email(project))
        heading = "New reply"
        opening = f"{activity.author_name} replied on “{project.title}”:"
        subject = f"New reply on {project.title}"
    else:
        recipients = {project.client.email}
        recipients.update(_team_emails(project))
        recipients.update(_project_lead_email(project))
        heading = f"New update · {activity.get_kind_display()}"
        opening = f"{activity.author_name} posted an update on “{project.title}”:"
        subject = f"New update on {project.title}"

    recipients.discard(author_email)
    if not recipients:
        return

    paragraphs = [opening, f"“{activity.text}”"]
    # Say what it's about. A comment anchored to a file reads very differently
    # from the same words floating in a feed, and the mail should carry that.
    anchor = activity.attachment
    if anchor is not None:
        paragraphs.insert(1, f"About: {anchor.label or anchor.display_name}")
    if is_reply and activity.parent is not None:
        paragraphs.append(
            f"In reply to {activity.parent.author_name}: "
            f"“{activity.parent.text[:160]}”"
        )

    send_brand_email(
        subject=subject,
        to=recipients,
        heading=heading,
        paragraphs=paragraphs,
        cta=("View the project", _project_url(project)),
    )


@_safe
def notify_submitted_for_review(project, is_resubmission=False):
    """Tell the client the work is theirs to look at.

    Resubmission gets its own wording. A client who asked for changes and then
    received the stock "your project is ready for review" has no way to tell
    whether their note was acted on or ignored.
    """
    if is_resubmission:
        send_brand_email(
            subject=f"Your changes are done: {project.title}",
            to=project.client.email,
            heading="The changes you asked for are ready",
            paragraphs=[
                f"Hi {project.client.full_name or 'there'},",
                f"The team has made the changes you requested on “{project.title}” "
                "and sent it back for you to look at.",
                "Approve the delivery when you're happy, or ask for more changes if "
                "something still isn't right.",
            ],
            cta=("Review the changes", _project_url(project)),
        )
        return
    send_brand_email(
        subject=f"Ready for your review: {project.title}",
        to=project.client.email,
        heading="Your project is ready for review",
        paragraphs=[
            f"Hi {project.client.full_name or 'there'},",
            f"“{project.title}” has been completed and submitted for your review.",
            "Take a look and approve the delivery when you're happy — that's when funds are released to the talent.",
            "If something isn't right, you can ask the team for changes instead.",
        ],
        cta=("Review & approve", _project_url(project)),
    )


@_safe
def notify_changes_requested(project, revision):
    """Tell the delivery team the client sent the work back.

    Goes to the lead and every assigned expert, because "who is meant to act on
    this?" has no single answer on a team — and the note itself is carried in
    the mail rather than linked to, so nobody has to open the app to find out
    how bad it is.
    """
    recipients = set(_project_lead_email(project)) | _team_emails(project)
    if not recipients:
        return
    round_label = (
        "" if project.revision_rounds <= 1
        else f" This is change request #{project.revision_rounds} on this project."
    )
    send_brand_email(
        subject=f"Changes requested: {project.title}",
        to=sorted(recipients),
        heading="The client has asked for changes",
        paragraphs=[
            f"{_client_name(project)} has sent “{project.title}” back for changes, "
            f"and it has moved to In Progress.{round_label}",
            f"What they said: “{revision.note}”",
            "Work through it, then submit for review again — that's what closes the "
            "request. Nothing already approved and paid has been reversed.",
        ],
        cta=("Open the project", _project_url(project)),
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


@_safe
def notify_task_edited(task, summary):
    """Tell whoever holds a task that its terms changed.

    Only them. What a task pays is between the platform and the person doing
    it, and the client already sees the change in the project's activity feed.
    """
    if not task.assignee:
        return
    send_brand_email(
        subject=f"Your task changed: {task.title}",
        to=task.assignee.email,
        heading="A task you're holding was updated",
        paragraphs=[
            f"Hi {task.assignee.full_name or 'there'},",
            f"Your delivery lead updated a task on “{task.project.title}”:",
            summary,
            "Open the project to see where it stands now.",
        ],
        cta=("Open the project", _project_url(task.project)),
    )


@_safe
def notify_task_reassigned(task, previous, current, note=""):
    """Tell both people. Losing work and gaining it are equally worth knowing.

    Addressed separately rather than as a pair — the two need to read different
    things, and neither needs the other's address.
    """
    project = task.project
    worth = (f" It's worth {_money2(task.amount_usd)} on approval."
             if task.amount_usd > 0 else "")
    tail = [f"Your delivery lead's note: “{note}”"] if note else []

    if current:
        send_brand_email(
            subject=f"A task moved to you: {task.title}",
            to=current.email,
            heading="You've picked up a task",
            paragraphs=[
                f"Hi {current.full_name or 'there'},",
                f"“{task.title}” on “{project.title}” is yours now.{worth}",
                *tail,
                "Submit it for review when it's done.",
            ],
            cta=("Open the project", _project_url(project)),
        )
    if previous:
        moved_to = ((current.full_name or current.email) if current
                    else "nobody for the moment")
        send_brand_email(
            subject=f"A task moved off your list: {task.title}",
            to=previous.email,
            heading="A task is no longer yours",
            paragraphs=[
                f"Hi {previous.full_name or 'there'},",
                f"“{task.title}” on “{project.title}” has been reassigned to "
                f"{moved_to}, so it's off your board.",
                *tail,
                "Anything you've already been approved and paid for is unaffected.",
            ],
            cta=("Open the project", _project_url(project)),
        )


@_safe
def notify_task_removed(project, title, assignee, reason, amount=0):
    """A task taken off the list — the client and whoever held it both hear why.

    The client because it changes what's being delivered; the expert because it
    takes work, and possibly money, off them. The amount goes only to the
    expert.
    """
    if assignee:
        worth = (f" It would have paid {_money2(amount)} on approval."
                 if amount and amount > 0 else "")
        send_brand_email(
            subject=f"A task was removed: {title}",
            to=assignee.email,
            heading="A task was taken off your list",
            paragraphs=[
                f"Hi {assignee.full_name or 'there'},",
                f"“{title}” on “{project.title}” has been removed.{worth}",
                f"Your delivery lead's reason: “{reason}”",
                "If that doesn't look right, reply to this email and we'll look into it.",
            ],
            cta=("Open the project", _project_url(project)),
        )
    send_brand_email(
        subject=f"Scope changed on {project.title}",
        to=project.client.email,
        heading="A piece of work was removed",
        paragraphs=[
            f"Hi {project.client.full_name or 'there'},",
            f"“{title}” has been removed from the plan for “{project.title}”.",
            f"The delivery lead's reason: “{reason}”",
            "Your quote is unchanged. Get in touch if this isn't what you expected.",
        ],
        cta=("View the project", _project_url(project)),
    )


@_safe
def notify_project_cancelled(project, refund=None):
    """Tell everyone attached that the work has stopped, and why.

    Goes to the client and the whole delivery team, whoever pulled the trigger.
    A cancellation someone finds out about by noticing a project missing from
    their board is the worst way to learn it.
    """
    recipients = ({project.client.email}
                  | set(_project_lead_email(project))
                  | _team_emails(project))
    if not recipients:
        return
    paragraphs = [
        f"“{project.title}” ({project.code}) has been cancelled.",
        f"Reason given: “{project.cancellation_reason}”",
    ]
    if refund is not None:
        paragraphs.append(
            f"A refund of {_money(refund.amount_usd)} has been raised on it — "
            "the client will get a separate note about that."
        )
    paragraphs.append(
        "Any work already approved and paid stays paid. Nothing has been "
        "reversed for the team."
    )
    send_brand_email(
        subject=f"Project cancelled: {project.title}",
        to=sorted(recipients),
        heading="This project has been cancelled",
        paragraphs=paragraphs,
        cta=("Open the project", _project_url(project)),
    )


@_safe
def notify_change_order_raised(project, order):
    """Ask the client to pay for extra scope.

    Only the client — nobody else needs telling that a bill was drafted, and the
    team hears when it's actually paid, which is the point at which their pool
    grows.
    """
    send_brand_email(
        subject=f"Extra work to approve: {project.title}",
        to=project.client.email,
        heading="There's extra work to approve",
        paragraphs=[
            f"Hi {project.client.full_name or 'there'},",
            f"The team has priced some additional work on “{project.title}” at "
            f"{_money(order.amount_usd)}.",
            f"What it covers: “{order.description}”",
            "Nothing changes until you pay for it — your original quote is "
            "unaffected, and the work already underway carries on either way.",
        ],
        cta=("Review & pay", _project_url(project)),
    )


@_safe
def notify_change_order_paid(project, order):
    """Tell the delivery team the extra scope is funded and the pool has grown."""
    recipients = set(_project_lead_email(project)) | _team_emails(project)
    if not recipients:
        return
    send_brand_email(
        subject=f"Extra scope paid: {project.title}",
        to=sorted(recipients),
        heading="The client paid for the extra work",
        paragraphs=[
            f"{_client_name(project)} has paid {_money(order.amount_usd)} for "
            f"additional work on “{project.title}”.",
            f"What it covers: “{order.description}”",
            "The expert pool has grown by the usual share of that, so there's "
            "more to price the new tasks against.",
        ],
        cta=("Open the project", _project_url(project)),
    )


@_safe
def notify_feedback_left(project, feedback):
    """Tell the lead what their client said. Only the lead.

    Not the experts: this is the client's private word about the engagement,
    and a 2/5 landing in four inboxes is a way to lose a team over a problem
    the lead hasn't had a chance to look at yet.
    """
    recipients = _project_lead_email(project)
    if not recipients:
        return
    stars = "★" * feedback.rating + "☆" * (5 - feedback.rating)
    paragraphs = [
        f"{_client_name(project)} rated “{project.title}” {feedback.rating}/5.  {stars}",
    ]
    if feedback.comment:
        paragraphs.append(f"What they said: “{feedback.comment}”")
    if feedback.would_work_again is False:
        paragraphs.append(
            "They said they would not work with this team again — worth a "
            "conversation before it becomes a lost client."
        )
    send_brand_email(
        subject=f"Client feedback on {project.title}",
        to=recipients,
        heading="Your client left feedback",
        paragraphs=paragraphs,
        cta=("Open the project", _project_url(project)),
    )


@_safe
def notify_cycle_raised(cycle):
    """Tell the client their next month is ready to pay.

    Sent a week ahead of the period, so paying is a task with a deadline rather
    than a surprise. The team hears nothing yet — there is no work to do until
    the money lands.
    """
    engagement = cycle.engagement
    send_brand_email(
        subject=f"Your {cycle.period_start:%B} invoice: {engagement.title}",
        to=cycle.client.email,
        heading=f"{cycle.period_start:%B %Y} is ready",
        paragraphs=[
            f"Hi {cycle.client.full_name or 'there'},",
            f"Your next month of “{engagement.title}” runs from "
            f"{cycle.period_start:%-d %B} to {cycle.period_end:%-d %B} and comes "
            f"to {_money(cycle.quote_usd)}.",
            "Paying starts the month. Nothing changes on the work already "
            "delivered.",
        ],
        cta=("Review & pay", _project_url(cycle)),
    )


@_safe
def notify_engagement_ended(engagement, final_cycle=None):
    """Tell everyone a retainer has stopped, and what happens to the last month."""
    recipients = {engagement.client.email}
    if engagement.lead_id and engagement.lead:
        recipients.add(engagement.lead.email)
    if final_cycle:
        recipients |= _team_emails(final_cycle)

    paragraphs = [
        f"“{engagement.title}” has ended and no further months will be billed.",
    ]
    if engagement.end_reason:
        paragraphs.append(f"Reason given: “{engagement.end_reason}”")
    if final_cycle and final_cycle.stage not in Project.CLOSED_STAGES:
        paragraphs.append(
            f"The current month runs to {final_cycle.period_end:%-d %B} and is "
            "unaffected — it's paid for, and it will be delivered."
        )
    send_brand_email(
        subject=f"Retainer ended: {engagement.title}",
        to=sorted(recipients),
        heading="This retainer has ended",
        paragraphs=paragraphs,
    )
