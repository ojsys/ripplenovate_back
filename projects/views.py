import logging

from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, Q
from django.http import FileResponse, Http404
from django.utils import timezone

from accounts.uploads import validate_project_document
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import notifications
from .access import (
    can_access_project,
    is_project_expert,
    leads_project,
    visible_projects,
)
from .models import (
    Activity,
    Attachment,
    ChangeOrder,
    Engagement,
    Project,
    ProjectFeedback,
    RevisionRequest,
    Task,
)
from .serializers import (
    ActivityCreateSerializer,
    AssignSerializer,
    AttachmentCreateSerializer,
    AttachmentSerializer,
    ChangeOrderCreateSerializer,
    ChangeOrderSerializer,
    EngagementCreateSerializer,
    EngagementSerializer,
    ExpertListSerializer,
    ProjectCreateSerializer,
    ProjectDetailSerializer,
    ProjectEditSerializer,
    ProjectFeedbackSerializer,
    ProjectListSerializer,
    QuoteSerializer,
    TaskEditSerializer,
    TaskReassignSerializer,
    TaskSerializer,
    TaskWriteSerializer,
)

User = get_user_model()
Role = User.Role
Stage = Project.Stage
logger = logging.getLogger("ripple")


# Short, human names for the fields a lead can edit, used to describe an edit in
# the activity feed.
EDIT_FIELD_LABELS = {
    "title": "title",
    "category": "service",
    "timeline": "timeline",
    "budget_range": "budget range",
    "target_date": "target date",
}


def describe_edit(project, changes):
    """Turn a {field: (old, new)} diff into one readable activity sentence."""
    parts = []
    # Money first — it's the change people care about most.
    if "quote_usd" in changes:
        old, new = changes["quote_usd"]
        parts.append(f"re-priced it from ${old:,} to ${new:,}")
    for field, label in EDIT_FIELD_LABELS.items():
        if field in changes:
            old, new = changes[field]
            if field == "target_date":
                # Dates read badly as ISO strings in a sentence.
                fmt = lambda d: d.strftime("%-d %b %Y") if d else "not set"
                parts.append(f"moved the target date from {fmt(old)} to {fmt(new)}")
            else:
                parts.append(
                    f"changed the {label} from “{old or '—'}” to “{new or '—'}”")
    if "description" in changes:
        parts.append("revised the brief")

    if not parts:
        return ""
    if len(parts) == 1:
        detail = parts[0]
    else:
        detail = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    return f"Edited the project — {detail}."


def credit_earnings(project):
    """Credit the expert / lead shares once the client approves delivery.

    Never breaks the approval: the earnings read path backfills any project that
    slipped through, so a failure here is self-healing.
    """
    # Local imports keep the app dependency one-way (payments → projects).
    from payments import earnings as earnings_service
    from payments import notifications as payout_notifications

    try:
        credited = earnings_service.record_project_earnings(project)
    except Exception as exc:  # noqa: BLE001 - approval must not fail on payout math
        logger.error("crediting earnings for %s failed: %s", project.code, exc)
        return
    # Earmark the platform's refund reserve from what it kept on this project.
    # After crediting, deliberately: the reserve is a slice of the *remainder*,
    # so it can't be worked out until everyone else has been paid. Wrapped
    # separately so a reserve failure can't cost anyone their earning.
    try:
        from payments import refunds as refund_service

        refund_service.contribute(project)
    except Exception as exc:  # noqa: BLE001
        logger.error("reserve contribution for %s failed: %s", project.code, exc)
    for earning in credited:
        payout_notifications.notify_earning_credited(
            earning.user, project, earning.amount_usd
        )


def log_activity(project, user, text, kind=Activity.Kind.SYSTEM):
    return Activity.objects.create(
        project=project,
        author=user,
        author_name=user.full_name or user.email,
        role_label=user.role_label,
        kind=kind,
        text=text,
    )


class ProjectViewSet(mixins.ListModelMixin,
                     mixins.RetrieveModelMixin,
                     mixins.CreateModelMixin,
                     viewsets.GenericViewSet):
    """Projects are listed, read, and created here; every state change after that
    goes through one of the lifecycle actions below.

    Deliberately **not** a ModelViewSet. A blanket PUT/PATCH would expose every
    field of ProjectDetailSerializer — `stage`, `quote_usd`, `expert` — to
    anyone the queryset lets through, so a client could mark their own brief
    Completed (or re-price it) without a quote, a payment, or an approval. Those
    writes bypass log_activity(), so nothing lands in the activity feed, no
    notification goes out, and no earnings are credited: the project changes and
    the change is never recorded. DELETE was likewise open, cascading away the
    project's tasks, activity, payments, and earnings.
    """

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        base = Project.objects.select_related(
            "client", "expert", "product_line", "service"
        ).prefetch_related("experts", "tasks", "activity",
                           "activity__attachments", "activity__replies",
                           "activity__replies__attachments", "attachments")
        if user.is_superuser:
            return base
        if user.role == Role.DELIVERY_LEAD:
            # Work they lead, plus unclaimed briefs in the disciplines they run
            # — the intake queue, which is the only way a brief ever gets
            # quoted. Once someone quotes it they own it and it leaves every
            # other lead's board. Keeping `lead=user` unconditional also means a
            # lead never loses sight of a project because an admin changed which
            # lines they cover.
            lines = user.product_lines.values_list("id", flat=True)
            return base.filter(
                Q(lead=user) | (Q(lead__isnull=True) & Q(product_line__in=lines))
            ).distinct()
        if user.role == Role.EXPERT:
            # Either shape of membership: on the team, or named as the primary
            # expert. The two are kept in step, but an admin editing a project
            # directly can set one without the other.
            return base.filter(Q(experts=user) | Q(expert=user)).distinct()
        # A client sees their own briefs plus everything their company has
        # bought. `client=user` stays in the clause rather than being replaced
        # by the org lookup: a project posted before organisations existed, or
        # one an admin created directly, may have no organisation at all, and
        # its author must never lose sight of it.
        orgs = user.organisation_memberships.values_list("organisation_id", flat=True)
        return base.filter(
            Q(client=user) | Q(organisation__in=list(orgs))
        ).distinct()

    def get_serializer_class(self):
        if self.action == "list":
            return ProjectListSerializer
        if self.action == "create":
            return ProjectCreateSerializer
        return ProjectDetailSerializer

    def _detail(self, project, status_code=status.HTTP_200_OK):
        """Serialize a FRESH copy so newly created tasks/activity are included.

        get_object() prefetches tasks/activity; rows created during the request
        aren't in that cache, so we re-fetch to return current data.
        """
        fresh = Project.objects.select_related(
            "client", "expert", "product_line", "service", "feedback"
        ).prefetch_related(
            "experts", "tasks", "activity", "activity__attachments",
            "activity__replies", "activity__replies__attachments", "attachments",
            "change_orders", "revision_requests", "refunds", "payments",
        ).get(pk=project.pk)
        # The request has to reach the serializer: who may read the client's
        # private feedback is decided from it. Without this everyone — including
        # the lead it's written for — sees null.
        return Response(
            ProjectDetailSerializer(fresh, context={"request": self.request}).data,
            status=status_code,
        )

    def create(self, request, *args, **kwargs):
        if request.user.role != Role.CLIENT:
            raise PermissionDenied("Only clients can post projects.")
        serializer = ProjectCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        references = serializer.validated_data.pop("references", [])
        # Attach the brief to the company that's buying it, so colleagues can
        # see it. A billing-only seat may not post — they're here to settle
        # invoices, not to commission work.
        seat = request.user.organisation_memberships.select_related(
            "organisation").first()
        if seat and not seat.sees_delivery:
            raise PermissionDenied(
                "Your seat at this company is billing-only, so you can't post a "
                "brief. Ask an owner to change your access."
            )
        organisation = seat.organisation if seat else None
        project = serializer.save(
            client=request.user,
            organisation=organisation,
            # The company name at the time of posting, exactly as `category`
            # snapshots the service — a later rename must not rewrite an
            # invoice that has already been settled.
            company=(organisation.name if organisation else request.user.company),
            stage=Stage.SUBMITTED,
            # Attribution follows the client's referral. It stays editable by a
            # lead until the client pays, and is locked from then on.
            business_developer=request.user.referred_by,
        )
        for ref in references:
            Attachment.objects.create(
                project=project, url=ref["url"], label=ref.get("label", ""),
                purpose=Attachment.Purpose.REFERENCE, added_by=request.user,
            )
        log_activity(project, request.user, "Submitted the project brief. Awaiting a quote.")
        notifications.notify_project_submitted(project)
        return self._detail(project, status.HTTP_201_CREATED)

    def _require_lead(self):
        user = self.request.user
        if user.role != Role.DELIVERY_LEAD and not user.is_superuser:
            raise PermissionDenied("Only a delivery lead can do that.")
        # A lead whose application is still in review can see their board but
        # can't put a price on client work yet.
        if not user.is_approved:
            raise PermissionDenied(
                "Your delivery lead account is still being reviewed. You'll be able "
                "to quote and assign work as soon as it's approved."
            )

    @action(detail=True, methods=["post"])
    def quote(self, request, pk=None):
        project = self.get_object()
        self._require_lead()
        if project.stage != Stage.SUBMITTED:
            raise ValidationError("This project has already been quoted.")
        serializer = QuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project.quote_usd = serializer.validated_data["quote_usd"]
        project.stage = Stage.QUOTED
        # The lead who quotes owns the brief, and earns the lead share on delivery.
        project.lead = request.user
        project.save(update_fields=["quote_usd", "stage", "lead"])
        log_activity(project, request.user,
                     f"Sent a quote of ${project.quote_usd:,} — ready for payment.")
        notifications.notify_quote_sent(project)
        return self._detail(project)

    @action(detail=True, methods=["patch"])
    def edit(self, request, pk=None):
        """Delivery lead fixes a brief or re-prices a quote — and it's recorded.

        The quote is frozen once the client has paid: the invoice they settled and
        the earnings credited on approval are both derived from it, so a later
        change would quietly disagree with money that has already moved.
        """
        project = self.get_object()
        self._require_lead()
        serializer = ProjectEditSerializer(project, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updates = serializer.validated_data

        if "quote_usd" in updates and project.is_paid:
            raise ValidationError(
                "This project is already paid — the quote is locked. "
                "Everything else on the brief can still be edited."
            )
        if "quote_usd" in updates and project.stage == Stage.SUBMITTED:
            raise ValidationError(
                "This brief hasn't been quoted yet — use Send quote instead."
            )

        # Diff before saving so the activity entry can name what actually changed.
        changes = {
            field: (getattr(project, field), value)
            for field, value in updates.items()
            if getattr(project, field) != value
        }
        if not changes:
            return self._detail(project)

        for field, (_old, new) in changes.items():
            setattr(project, field, new)
        project.save(update_fields=list(changes))

        summary = describe_edit(project, changes)
        log_activity(project, request.user, summary)
        notifications.notify_project_edited(
            project, summary, repriced="quote_usd" in changes
        )
        return self._detail(project)

    @action(detail=True, methods=["post"], url_path="attribute")
    def attribute(self, request, pk=None):
        """Set or clear the business developer credited with sourcing this brief.

        **Locked once the client has paid.** The same rule the quote follows: the
        commission comes out of money that has already changed hands, so who
        receives it can't be rewritten afterwards. Before payment a lead can
        correct an attribution that the referral got wrong.
        """
        project = self.get_object()
        self._require_lead()
        if project.is_paid:
            raise ValidationError(
                "This project is already paid — the business developer is locked. "
                "The commission is part of a quote the client has settled."
            )

        raw = request.data.get("business_developer")
        if raw in (None, "", "null"):
            new_bd = None
        else:
            new_bd = User.objects.filter(id=raw, role=Role.BUSINESS_DEV).first()
            if not new_bd:
                raise ValidationError("Select a valid business developer.")

        if new_bd == project.business_developer:
            return self._detail(project)

        previous = project.business_developer
        project.business_developer = new_bd
        project.save(update_fields=["business_developer"])

        if new_bd:
            text = (f"Credited {new_bd.full_name or new_bd.email} as the business "
                    "developer on this project.")
        else:
            name = previous.full_name or previous.email if previous else "the business developer"
            text = f"Removed {name} as the business developer on this project."
        log_activity(project, request.user, text)
        notifications.notify_attribution_changed(project, previous, new_bd)
        return self._detail(project)

    @action(detail=True, methods=["post"], url_path="documents")
    def documents(self, request, pk=None):
        """Upload a document to the project.

        Open to the client, the assigned expert and the delivery lead — all
        three have something to hand over. Purpose follows the role for the same
        reason a link's does: a client supplies references, the delivery team
        supplies deliverables, so nobody can misstate who produced the work.
        """
        project = self.get_object()
        user = request.user
        is_client = user.role == Role.CLIENT and project.client_id == user.id
        is_team = (self._is_lead()
                   or (user.role == Role.EXPERT
                       and is_project_expert(user, project)))
        if not (is_client or is_team):
            raise PermissionDenied("You can't add documents to this project.")

        upload = request.FILES.get("file")
        if not upload:
            raise ValidationError("Choose a file to upload.")
        try:
            validate_project_document(upload)
        except DjangoValidationError as exc:
            raise ValidationError(" ".join(exc.messages))

        purpose = (Attachment.Purpose.REFERENCE if is_client
                   else request.data.get("purpose") or Attachment.Purpose.DELIVERABLE)
        attachment = Attachment.objects.create(
            project=project,
            file=upload,
            original_filename=upload.name[:255],
            size_bytes=upload.size,
            label=(request.data.get("label") or "").strip()[:200],
            purpose=purpose,
            added_by=user,
        )
        if purpose == Attachment.Purpose.DELIVERABLE:
            log_activity(project, user, f"Added a deliverable — {attachment.label}.")
        return Response(AttachmentSerializer(attachment).data,
                        status=status.HTTP_201_CREATED)

    def _resolve_experts(self, project, expert_ids):
        """Turn ids into experts who may actually take this brief.

        Someone from outside your roster has to cover the brief's discipline —
        you haven't vouched for them, so their product lines are all there is
        to judge by. Your own people you have vouched for, and that outranks
        the tag: it was copied from whichever lead signed them up, nobody
        curates it, and no lead can edit it, so enforcing it against your own
        roster only ever said "no" to a team you picked yourself.

        Runs per person rather than once — a team is only as valid as its
        least suitable member.
        """
        experts = []
        for raw in expert_ids:
            expert = User.objects.filter(id=raw, role=Role.EXPERT).first()
            if not expert:
                raise ValidationError("Select a valid expert.")
            if (project.product_line_id
                    and expert.lead_id != self.request.user.id
                    and not expert.product_lines
                                 .filter(id=project.product_line_id).exists()):
                raise ValidationError(
                    f"{expert.full_name or expert.email} doesn't work in "
                    f"{project.product_line.name} and isn't on your team. Add "
                    "them to your team first, or pick someone else."
                )
            experts.append(expert)
        return experts

    @staticmethod
    def _names(people):
        listed = [p.full_name or p.email for p in people]
        if len(listed) == 1:
            return listed[0]
        return ", ".join(listed[:-1]) + f" and {listed[-1]}"

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        """Put a team on a paid brief and start delivery.

        The first expert named is the primary — the one answerable for the whole
        thing, and the one a project with no priced tasks pays in full.
        """
        project = self.get_object()
        self._require_lead()
        if project.stage != Stage.PAID:
            raise ValidationError("An expert can only be assigned after payment.")
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        experts = self._resolve_experts(
            project, serializer.validated_data["expert_ids"])
        primary = experts[0]

        titles = [t.strip() for t in serializer.validated_data.get("tasks", []) if t.strip()]
        project.expert = primary
        project.stage = Stage.IN_PROGRESS
        # Covers briefs that were paid before leads were tracked on the project.
        if not project.lead_id:
            project.lead = request.user
        project.save(update_fields=["expert", "stage", "lead"])
        # The primary expert is always a member of the team too, so everything
        # that asks "who is delivering this?" has one place to look.
        project.experts.add(*experts)
        if titles:
            # Only ever clears the seed list. A task that has been paid can't be
            # deleted at all (the earning protects it), and none can exist yet —
            # this runs once, on the way out of Paid.
            project.tasks.filter(earnings__isnull=True).delete()
            Task.objects.bulk_create([
                Task(project=project, title=t, assignee=primary, order=i)
                for i, t in enumerate(titles)
            ])
        log_activity(project, request.user,
                     f"Assigned {self._names(experts)} and kicked off delivery.")
        notifications.notify_experts_assigned(project, experts)
        return self._detail(project)

    @action(detail=True, methods=["post"], url_path="tasks")
    def create_task(self, request, pk=None):
        """Add a task to the list, optionally priced and assigned."""
        project = self.get_object()
        self._require_lead()
        _check_project_open(project)

        serializer = TaskWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        _check_assignee(project, data.get("assignee"))
        _check_allocation(project, data.get("amount_usd"))

        if "order" not in data:
            last = project.tasks.order_by("-order").first()
            data["order"] = (last.order + 1) if last else 0
        task = Task.objects.create(project=project, **data)

        if task.amount_usd > 0 and task.assignee:
            log_activity(
                project, request.user,
                f"Added “{task.title}” for {task.assignee.full_name or task.assignee.email} "
                f"— ${task.amount_usd:,.2f} on approval."
            )
        return Response(TaskSerializer(task).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="experts")
    def add_experts(self, request, pk=None):
        """Add experts to a project already in delivery."""
        project = self.get_object()
        self._require_lead()
        if not project.is_paid:
            raise ValidationError(
                "Build the delivery team once the client has paid.")
        serializer = ExpertListSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        experts = self._resolve_experts(
            project, serializer.validated_data["expert_ids"])

        already = {e.id for e in project.experts.all()}
        added = [e for e in experts if e.id not in already]
        if not added:
            return self._detail(project)

        project.experts.add(*added)
        # A team with nobody answerable for it isn't a team. If the project has
        # no primary yet, the first person on it becomes one.
        if not project.expert_id:
            project.expert = added[0]
            project.save(update_fields=["expert"])
        log_activity(project, request.user,
                     f"Added {self._names(added)} to the delivery team.")
        notifications.notify_experts_assigned(project, added)
        return self._detail(project)

    @action(detail=True, methods=["delete"],
            url_path=r"experts/(?P<user_id>[^/.]+)")
    def remove_expert(self, request, pk=None, user_id=None):
        """Take an expert off the delivery team.

        Refused once they've been paid for work here, or hold a task with a
        price on it — money already credited can't be detached from the person
        it was credited to, and a priced task is a commitment to pay. Reassign
        or clear the task first, which is a decision for the lead to make
        deliberately rather than a side effect of removing someone.
        """
        project = self.get_object()
        self._require_lead()
        expert = next((e for e in project.experts.all() if str(e.id) == str(user_id)),
                      None)
        if not expert:
            raise ValidationError("That person isn't on this project's team.")

        if project.earnings.filter(user=expert).exists():
            raise ValidationError(
                f"{expert.full_name or expert.email} has already been paid for "
                "work on this project, so they stay on the record."
            )
        priced = project.tasks.filter(assignee=expert, amount_usd__gt=0)
        if priced.exists():
            raise ValidationError(
                f"{expert.full_name or expert.email} still holds "
                f"{priced.count()} priced task(s). Reassign them or clear their "
                "amounts first."
            )

        # Their remaining tasks carry no money, but leaving them pointing at
        # someone who's off the team would strand them. Unassigned is honest,
        # and the activity line says so rather than it happening quietly.
        orphaned = project.tasks.filter(assignee=expert).update(assignee=None)
        project.experts.remove(expert)
        if project.expert_id == expert.id:
            # Hand the primary role to whoever is left, or leave it empty.
            successor = project.experts.exclude(id=expert.id).first()
            project.expert = successor
            project.save(update_fields=["expert"])

        text = f"Removed {expert.full_name or expert.email} from the delivery team."
        if orphaned:
            text += f" {orphaned} task(s) are now unassigned."
        log_activity(project, request.user, text)
        return self._detail(project)

    def _is_lead(self):
        """A lead cleared to act on this project.

        `get_object()` has already scoped the project to this user, so what's
        left to check is approval — the same bar `_require_lead` sets. Without
        it a self-serve signup nobody has reviewed could hand work to a client,
        sign it off, and release the earnings that follow.
        """
        user = self.request.user
        if user.role != Role.DELIVERY_LEAD and not user.is_superuser:
            return False
        return user.is_approved

    @action(detail=True, methods=["post"], url_path="submit-review")
    def submit_review(self, request, pk=None):
        """Hand the work to the client. The assigned expert or the lead can do it —
        the lead often needs to move it along on the expert's behalf."""
        project = self.get_object()
        is_assigned_expert = (request.user.role == Role.EXPERT
                              and is_project_expert(request.user, project))
        if not (is_assigned_expert or self._is_lead()):
            raise PermissionDenied(
                "Only the assigned expert or a delivery lead can submit for review."
            )
        if project.stage != Stage.IN_PROGRESS:
            raise ValidationError("This project isn't in progress.")
        project.stage = Stage.REVIEW
        project.save(update_fields=["stage"])
        # Resubmitting is what answers an outstanding revision request. Closing
        # it here rather than making the team tick something off means the two
        # can't disagree — the work is back with the client either way.
        resubmission = project.revision_requests.filter(
            resolved_at__isnull=True).update(resolved_at=timezone.now())
        if resubmission:
            text = "Made the requested changes and sent the work back for review."
        else:
            text = ("Submitted the work for client review."
                    if is_assigned_expert else
                    "Moved the project to review and notified the client.")
        log_activity(project, request.user, text)
        notifications.notify_submitted_for_review(
            project, is_resubmission=bool(resubmission))
        return self._detail(project)

    @action(detail=True, methods=["post"], url_path="request-changes")
    def request_changes(self, request, pk=None):
        """Send delivered work back to the team, with a reason.

        The second exit from Review, and the one that was missing: until now a
        client who wasn't happy could only decline to click Approve, which
        communicated nothing and left the project parked.

        Money already released is deliberately untouched. Tasks the lead
        approved were paid from cash the client settled up front, and unwinding
        them here would turn every revision into a payment dispute. Rework is
        new tasks or reopened ones; a genuine failure is a refund (a different
        action, with its own decision).
        """
        project = self.get_object()
        is_client = (request.user.role == Role.CLIENT
                     and project.client_id == request.user.id)
        if not (is_client or request.user.is_superuser):
            raise PermissionDenied("Only the client can ask for changes.")
        if project.stage != Stage.REVIEW:
            raise ValidationError(
                "There's nothing to send back — this project isn't in review."
            )
        note = (request.data.get("note") or "").strip()
        if not note:
            raise ValidationError(
                "Say what needs changing, so the team knows what to fix."
            )

        revision = RevisionRequest.objects.create(
            project=project, requested_by=request.user, note=note)
        project.stage = Stage.IN_PROGRESS
        project.revision_rounds = F("revision_rounds") + 1
        project.save(update_fields=["stage", "revision_rounds"])
        project.refresh_from_db(fields=["revision_rounds"])

        log_activity(project, request.user, note, kind=Activity.Kind.REVISION)
        notifications.notify_changes_requested(project, revision)
        return self._detail(project)

    @action(detail=True, methods=["post"], url_path="remind-review")
    def remind_review(self, request, pk=None):
        """Nudge the client again while a project is sitting in review."""
        project = self.get_object()
        is_assigned_expert = (request.user.role == Role.EXPERT
                              and is_project_expert(request.user, project))
        if not (is_assigned_expert or self._is_lead()):
            raise PermissionDenied("Only the delivery team can send that reminder.")
        if project.stage != Stage.REVIEW:
            raise ValidationError("This project isn't waiting on the client's review.")
        # The first reminder starts the clock on closing this over a silent
        # client's head. Later reminders don't restart it — otherwise nudging
        # again would postpone the team's own payday.
        if not project.review_reminded_at:
            project.review_reminded_at = timezone.now()
            project.save(update_fields=["review_reminded_at"])
        log_activity(project, request.user,
                     "Reminded the client that the work is ready for review.")
        notifications.notify_review_reminder(project)
        return self._detail(project)

    def _complete(self, project, actor, *, by_client, countersigner=None):
        """Close a project out and release every earning on it. One path only.

        Completion is the single most consequential action in the product — it
        is what turns a quote into other people's money — so both routes to it
        run through here rather than each doing their own bookkeeping.
        """
        project.stage = Stage.COMPLETED
        project.completed_at = timezone.now()
        project.completed_by = actor
        project.countersigned_by = countersigner
        project.save(update_fields=["stage", "completed_at", "completed_by",
                                    "countersigned_by"])
        if by_client:
            text = "Approved delivery. Project complete!"
        elif countersigner:
            name = countersigner.full_name or countersigner.email
            text = f"Marked the project complete on the client's behalf, countersigned by {name}."
        else:
            text = ("Marked the project complete on the client's behalf, after "
                    "the client went quiet.")
        log_activity(project, actor, text)
        notifications.notify_project_completed(project, completed_by_client=by_client)
        credit_earnings(project)
        return self._detail(project)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Close the project out and release the earnings.

        The client signs their own work off, always, with no conditions.

        A delivery lead can also complete it, because a client who stops
        replying must not be able to strand the team's money indefinitely — but
        not instantly and not silently. That path now requires the client to
        have been reminded, to have actually gone quiet since, and to have no
        outstanding change request. Otherwise a lead could take payment for work
        the client had just rejected, which is the same shape of problem the
        two-person rule already solves for withdrawals.
        """
        project = self.get_object()
        is_client = (request.user.role == Role.CLIENT
                     and project.client_id == request.user.id)
        if not (is_client or self._is_lead()):
            raise PermissionDenied("Only the client or a delivery lead can complete this.")
        if project.stage != Stage.REVIEW:
            raise ValidationError("This project isn't ready for approval.")
        if not is_client:
            blocked = project.client_silence_block()
            if blocked:
                raise PermissionDenied(blocked)
        return self._complete(project, request.user, by_client=is_client)

    @action(detail=True, methods=["post"])
    def feedback(self, request, pk=None):
        """The client's verdict, once, on work that has finished.

        Only after the project is closed — either signed off or cancelled.
        Asking mid-delivery gets you a progress report, not a verdict, and a
        client who has already told you it's a 2 has no reason to keep working
        with the team on fixing it.
        """
        project = self.get_object()
        if project.client_id != request.user.id:
            raise PermissionDenied("Only the client can leave feedback.")
        if project.stage not in Project.CLOSED_STAGES:
            raise ValidationError(
                "There's nothing to rate yet — this project is still running."
            )
        if hasattr(project, "feedback"):
            raise ValidationError("You've already left feedback on this project.")
        serializer = ProjectFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(project=project)
        # Not written to the activity feed on purpose: the feed is read by the
        # experts, and this is the client's private word to the lead.
        notifications.notify_feedback_left(project, serializer.instance)
        return self._detail(project)

    @action(detail=True, methods=["get", "post"], url_path="change-orders")
    def change_orders(self, request, pk=None):
        """Extra scope on a project the client has already paid for.

        GET is open to anyone on the project — the client is being asked to pay
        for these, so they can hardly be hidden from them. POST is the lead's:
        pricing scope is their job, and it's their expert pool that grows.
        """
        project = self.get_object()
        if request.method == "GET":
            return Response(ChangeOrderSerializer(
                project.change_orders.all(), many=True).data)

        self._require_lead()
        if not leads_project(request.user, project):
            raise PermissionDenied("Only this project's delivery lead can add scope.")
        # Live paid work only. Before payment there's nothing to add to — edit
        # the quote. After completion the earnings are snapshotted, so extra
        # contract value would make the split disagree with the ledger.
        if project.stage not in (Stage.PAID, Stage.IN_PROGRESS, Stage.REVIEW):
            raise ValidationError(
                "Extra scope can only be added to a paid project that's still "
                "running. Re-price the quote instead, or raise a new brief."
            )
        serializer = ChangeOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = ChangeOrder.objects.create(
            project=project, raised_by=request.user,
            **serializer.validated_data,
        )
        log_activity(
            project, request.user,
            f"Raised a change order for ${order.amount_usd:,.2f}: {order.description}",
        )
        notifications.notify_change_order_raised(project, order)
        return Response(ChangeOrderSerializer(order).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Stop a project for good, and decide about the money in the same breath.

        Two quite different situations wear the same word. Before payment,
        cancelling is housekeeping — the client changed their mind, nothing has
        moved, anyone involved can call it off. After payment there is real
        cash against the project, so cancelling is a financial act: it's
        restricted to the delivery side, and it requires an explicit refund
        figure even when that figure is zero.

        Requiring the zero is the point. A cancelled paid project with no
        refund decision recorded is the exact state where a client believes
        they're owed something and the platform has no note of it.
        """
        project = self.get_object()
        if project.stage in Project.CLOSED_STAGES:
            raise ValidationError(
                f"This project is already {project.stage.lower()}."
            )

        is_client = (request.user.role == Role.CLIENT
                     and project.client_id == request.user.id)
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            raise ValidationError("Say why this project is being cancelled.")

        if not project.is_paid:
            if not (is_client or self._is_lead()):
                raise PermissionDenied("Only the client or a delivery lead can cancel this.")
            refund = None
        else:
            if not self._is_lead():
                raise PermissionDenied(
                    "This project has been paid for, so a delivery lead has to "
                    "cancel it and settle the refund. Ask your delivery lead."
                )
            raw = request.data.get("refund_usd")
            if raw is None or str(raw).strip() == "":
                raise ValidationError(
                    "Decide what to refund before cancelling a paid project. "
                    "Send refund_usd: 0 if nothing is owed."
                )
            try:
                amount = Decimal(str(raw))
            except (TypeError, ArithmeticError, ValueError):
                raise ValidationError("That refund amount isn't a number.")
            refund = self._raise_refund(project, request.user, amount, reason) \
                if amount > 0 else None

        project.stage = Stage.CANCELLED
        project.cancelled_at = timezone.now()
        project.cancelled_by = request.user
        project.cancellation_reason = reason
        project.save(update_fields=["stage", "cancelled_at", "cancelled_by",
                                    "cancellation_reason"])
        who = "the client" if is_client else "the delivery team"
        note = f"Cancelled the project ({who}): {reason}"
        if refund:
            note += f" A refund of ${refund.amount_usd:,.2f} was raised."
        log_activity(project, request.user, note)
        notifications.notify_project_cancelled(project, refund=refund)
        return self._detail(project)

    def _raise_refund(self, project, user, amount, reason):
        """Create a refund from a lifecycle action, translating service errors."""
        from payments import refunds as refund_service

        try:
            return refund_service.request_refund(project, user, amount, reason)
        except refund_service.RefundError as exc:
            raise ValidationError(str(exc))

    @action(detail=True, methods=["post"], url_path="countersign-completion")
    def countersign_completion(self, request, pk=None):
        """An administrator authorising completion without the wait.

        The escape hatch from the silence window, and the reason the window can
        afford to be strict: a genuinely abandoned project doesn't have to sit
        for a week, it just needs somebody with no stake in the lead share to
        agree.

        Administrators rather than peer leads, deliberately. The obvious design
        was a second delivery lead, but `access.visible_projects` hides a lead's
        project from every other lead on purpose — running the same discipline
        is not a reason to see someone else's client's budget. Routing the
        second pair of eyes through an admin, who can already see everything,
        gets the two-person rule without quietly widening that boundary. It
        also matches how settling withdrawals already works.
        """
        project = self.get_object()
        if not request.user.is_superuser:
            raise PermissionDenied(
                "Only an administrator can countersign a completion. Otherwise "
                "the project completes on its own once the client has been "
                "silent for the configured number of days."
            )
        if project.stage != Stage.REVIEW:
            raise ValidationError("This project isn't ready for approval.")
        if request.user.id == project.lead_id:
            raise PermissionDenied(
                "Countersigning is a second pair of eyes — it can't be your own. "
                "This is your project, so the silence window applies to you too."
            )
        # A countersignature overrides the wait, never the client's own words.
        if project.open_revision:
            raise PermissionDenied(
                "The client has asked for changes. Make them and resubmit — no "
                "countersignature can complete a project over an open change request."
            )
        lead = project.lead or request.user
        return self._complete(project, lead, by_client=False,
                              countersigner=request.user)

    @action(detail=True, methods=["post"])
    def activity(self, request, pk=None):
        """Post an update, reply to one, or comment on a deliverable.

        All three through one endpoint, because they are the same act with
        different context. A reply carries `parent`; a comment about a specific
        file carries `attachment`; a plain update carries neither.
        """
        project = self.get_object()
        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        text = serializer.validated_data["text"].strip()
        if not text:
            raise ValidationError("Write an update first.")

        parent = self._resolve_parent(project, serializer.validated_data.get("parent"))
        anchor = self._resolve_anchor(
            project, serializer.validated_data.get("attachment"))
        # A reply is a reply, not a Blocker or a Milestone. Typed kinds classify
        # the state of the work; a response inside a thread doesn't, and letting
        # one be typed would put a second Blocker chip in the feed for a comment
        # agreeing with the first.
        kind = (Activity.Kind.UPDATE if parent
                else serializer.validated_data["kind"])

        entry = log_activity(project, request.user, text, kind=kind)
        if parent or anchor:
            entry.parent = parent
            entry.attachment = anchor
            entry.save(update_fields=["parent", "attachment"])
        for link in serializer.validated_data.get("attachments", []):
            Attachment.objects.create(
                project=project, activity=entry, url=link["url"],
                label=link.get("label", ""), purpose=link.get("purpose"),
                added_by=request.user,
            )
        notifications.notify_update_posted(project, entry)
        return self._detail(project)

    def _resolve_parent(self, project, parent_id):
        """The entry being replied to, flattened to one level.

        Replying to a reply attaches to its parent rather than nesting. The feed
        stays two levels deep whatever the client sends, so the renderer never
        has to handle a tree and `thread_emails` never has to walk one.
        """
        if not parent_id:
            return None
        parent = project.activity.filter(id=parent_id).first()
        if not parent:
            # Deliberately not a 404 about the id: an activity on someone
            # else's project is not this caller's business to confirm exists.
            raise ValidationError("That update isn't on this project.")
        return parent.parent or parent

    def _resolve_anchor(self, project, attachment_id):
        if not attachment_id:
            return None
        anchor = project.attachments.filter(id=attachment_id).first()
        if not anchor:
            raise ValidationError("That file isn't on this project.")
        return anchor

    @action(detail=True, methods=["post"])
    def attachments(self, request, pk=None):
        """Attach a link to the project, outside of an update.

        Who may attach what follows who is doing the work: the client supplies
        references on their own brief, the delivery team hands over
        deliverables. A client marking something a "deliverable" would misstate
        who produced it, so the purpose is derived from the role rather than
        trusted from the request.
        """
        project = self.get_object()
        user = request.user
        is_client = user.role == Role.CLIENT and project.client_id == user.id
        is_team = (self._is_lead()
                   or (user.role == Role.EXPERT
                       and is_project_expert(user, project)))
        if not (is_client or is_team):
            raise PermissionDenied("You can't add links to this project.")

        serializer = AttachmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purpose = (Attachment.Purpose.REFERENCE if is_client
                   else serializer.validated_data["purpose"])
        attachment = Attachment.objects.create(
            project=project,
            url=serializer.validated_data["url"],
            label=serializer.validated_data.get("label", ""),
            purpose=purpose,
            added_by=user,
        )
        # A deliverable landing is worth recording — it's part of the handover,
        # not an incidental edit.
        if purpose == Attachment.Purpose.DELIVERABLE:
            log_activity(project, user, f"Added a deliverable — {attachment.label}.")
        return Response(AttachmentSerializer(attachment).data,
                        status=status.HTTP_201_CREATED)


def _writable_task(request, task_id):
    """Fetch a task the requesting lead is allowed to change, or raise.

    Only a delivery lead running the project maintains its task list. Experts
    move their own tasks through the lifecycle instead — a separate thing, with
    separate rules, because that path is what releases money.
    """
    task = (Task.objects
            .select_related("project", "project__expert", "project__product_line")
            .filter(id=task_id)
            .first())
    if not task:
        raise Http404
    if not leads_project(request.user, task.project):
        raise PermissionDenied("You can't change tasks on this project.")
    return task


def _check_task_writable(task):
    """A task stops being editable once the expert has handed it in.

    Re-pricing work someone has already done — or already been paid for — is
    changing the deal after the fact. Send it back with `request-changes` if it
    needs more work; that returns it to the lead's control honestly.
    """
    if task.status == Task.Status.APPROVED:
        raise ValidationError(
            "This task is approved and settled — its details are fixed.")
    if task.status == Task.Status.SUBMITTED:
        raise ValidationError(
            "This task is waiting on your review. Approve it, or request "
            "changes, before editing it."
        )


def _check_allocation(project, amount, replacing=Decimal("0")):
    """Keep the task amounts inside the project's expert share.

    The invariant the whole feature rests on: the platform can't be made to pay
    out more than it took in. `replacing` is the amount this write is
    overwriting, so editing a task counts its own old value as freed.
    """
    amount = Decimal(amount or 0)
    if amount <= 0:
        return
    pool = project.expert_pool_usd
    committed = project.allocated_usd - Decimal(replacing or 0)
    if committed + amount > pool:
        remaining = pool - committed
        raise ValidationError(
            f"That's more than this project has left to allocate. The expert "
            f"share is ${pool:,.2f} and ${remaining:,.2f} is unallocated."
        )


def _check_assignee(project, assignee):
    """Tasks go to people who are actually on the project."""
    if assignee and not is_project_expert(assignee, project):
        raise ValidationError(
            f"{assignee.full_name or assignee.email} isn't on this project's "
            "delivery team. Add them to the team first."
        )


def _check_project_open(project):
    if not project.is_paid:
        raise ValidationError("Tasks are set up once the client has paid.")
    if project.stage == Stage.COMPLETED:
        raise ValidationError(
            "This project is complete — its task list is part of the record now.")


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def task_detail(request, task_id):
    """Maintain one task on a project's list — the delivery lead's job."""
    if request.method == "DELETE":
        return _delete_task(request, task_id)
    return _update_task(request, task_id)


TASK_FIELD_LABELS = {
    "title": "title",
    "description": "brief",
    "order": "position",
}


def describe_task_edit(task, changes):
    """One readable sentence for what a lead changed about a task.

    Task edits used to be silent — no feed entry, no email. The person doing
    the work could find their task retitled, re-scoped or re-priced and learn
    about it only by looking. What a task is worth and what it asks for is the
    deal between the platform and the expert; changing it without saying so
    isn't a change, it's a surprise.
    """
    parts = []
    if "amount_usd" in changes:
        old, new = changes["amount_usd"]
        parts.append(f"changed what it pays from ${old:,.2f} to ${new:,.2f}")
    for field, label in TASK_FIELD_LABELS.items():
        if field not in changes:
            continue
        old, new = changes[field]
        if field == "description":
            parts.append("revised the brief")
        elif field == "order":
            continue  # reordering isn't news
        else:
            parts.append(f"changed the {label} from “{old}” to “{new}”")
    if not parts:
        return ""
    detail = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return f"Edited “{task.title}” — {detail}."


def _update_task(request, task_id):
    """Retitle, re-scope or re-price a task that hasn't been handed in yet.

    Who holds it moves through `reassign_task` instead.
    """
    task = _writable_task(request, task_id)
    project = task.project
    _check_project_open(project)
    _check_task_writable(task)

    serializer = TaskEditSerializer(task, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    updates = serializer.validated_data

    if "amount_usd" in updates:
        _check_allocation(project, updates["amount_usd"], replacing=task.amount_usd)

    # Diff before saving so the feed entry can name what actually changed, and
    # so an edit that changes nothing stays silent.
    changes = {
        field: (getattr(task, field), value)
        for field, value in updates.items()
        if getattr(task, field) != value
    }
    if not changes:
        return Response(TaskSerializer(task).data)

    for field, (_old, new) in changes.items():
        setattr(task, field, new)
    task.save(update_fields=list(changes))

    summary = describe_task_edit(task, changes)
    if summary:
        log_activity(project, request.user, summary)
        notifications.notify_task_edited(task, summary)
    return Response(TaskSerializer(task).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reassign_task(request, task_id):
    """Move a task to someone else on the team, or take it off everyone.

    Allowed on work already handed in, unlike re-pricing: an expert going quiet
    mid-review is exactly when a lead needs to move the work, and refusing
    would leave the task stuck with the one person who can't finish it. A
    submitted task goes back to "to do" on the way, because the submission
    belonged to whoever made it — the new holder hasn't handed anything in.

    An approved task doesn't move. It's paid, and the ledger records who was
    paid for it.
    """
    task = _task_or_404(task_id)
    if not task:
        raise Http404
    project = task.project
    if not leads_project(request.user, project):
        raise PermissionDenied("Only the delivery lead running this project can move a task.")
    _check_project_open(project)
    if task.status == Task.Status.APPROVED:
        raise ValidationError(
            "This task is approved and paid — it stays with the person who did it."
        )

    serializer = TaskReassignSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    raw = serializer.validated_data.get("assignee")
    note = (serializer.validated_data.get("note") or "").strip()

    new_assignee = None
    if raw is not None:
        new_assignee = User.objects.filter(id=raw, role=Role.EXPERT).first()
        if not new_assignee:
            raise ValidationError("Select a valid expert.")
        _check_assignee(project, new_assignee)

    previous = task.assignee
    if previous == new_assignee:
        return Response(TaskSerializer(task).data)

    task.assignee = new_assignee
    fields = ["assignee"]
    if task.status == Task.Status.SUBMITTED:
        task.status = Task.Status.TODO
        task.submitted_at = None
        fields += ["status", "submitted_at"]
    task.save(update_fields=fields)

    who = lambda u: (u.full_name or u.email) if u else None
    if new_assignee and previous:
        text = f"Moved “{task.title}” from {who(previous)} to {who(new_assignee)}."
    elif new_assignee:
        text = f"Assigned “{task.title}” to {who(new_assignee)}."
    else:
        text = f"Unassigned “{task.title}” — it was {who(previous)}'s."
    if note:
        text += f" {note}"
    log_activity(project, request.user, text)
    notifications.notify_task_reassigned(task, previous, new_assignee, note)
    return Response(TaskSerializer(task).data)


def _task_or_404(task_id):
    return (Task.objects
            .select_related("project", "project__client", "project__expert",
                            "assignee")
            .filter(id=task_id)
            .first())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_task(request, task_id):
    """Hand a task in for the lead to check.

    The assignee only — not the lead "on their behalf". A lead who could both
    submit and approve would be the whole chain on their own, and approval is
    what releases the money.

    If an expert goes quiet, the way through is to reassign the task while it's
    still open, not to submit for them.
    """
    task = _task_or_404(task_id)
    if not task:
        raise Http404
    if task.assignee_id != request.user.id:
        raise PermissionDenied("Only the person a task is assigned to can hand it in.")
    if task.status == Task.Status.APPROVED:
        raise ValidationError("This task has already been approved.")
    if task.status == Task.Status.SUBMITTED:
        raise ValidationError("This task is already waiting on review.")

    task.status = Task.Status.SUBMITTED
    task.submitted_at = timezone.now()
    task.save(update_fields=["status", "submitted_at"])
    log_activity(task.project, request.user,
                 f"Submitted “{task.title}” for review.")
    notifications.notify_task_submitted(task)
    return Response(TaskSerializer(task).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_task(request, task_id):
    """Sign a task off — and pay for it.

    Terminal, deliberately. The earning it writes is withdrawable immediately,
    so there is no coming back from here; a lead who isn't sure should request
    changes instead.
    """
    from payments import earnings as earnings_service
    from payments import notifications as payout_notifications

    task = _task_or_404(task_id)
    if not task:
        raise Http404
    project = task.project
    if not leads_project(request.user, project):
        raise PermissionDenied("Only the delivery lead running this project can approve a task.")
    # The rule payouts already follow one step further down the chain, where
    # nobody settles their own withdrawal. A lead holding a task of their own
    # needs another lead to sign it off.
    if task.assignee_id == request.user.id:
        raise PermissionDenied(
            "You can't approve your own task — ask another delivery lead or an admin."
        )
    if task.status == Task.Status.APPROVED:
        raise ValidationError("This task is already approved.")
    if task.status != Task.Status.SUBMITTED:
        raise ValidationError(
            "This task hasn't been handed in yet. It can be approved once the "
            "expert submits it for review."
        )
    if not project.is_paid:
        raise ValidationError("Nothing is paid out before the client has.")

    # The status change and the payment are one thing. If crediting fails the
    # approval has to fail with it — a task that reads approved but was never
    # paid is the worst of both, and it's terminal, so nothing would retry it.
    with transaction.atomic():
        task.status = Task.Status.APPROVED
        task.approved_at = timezone.now()
        task.approved_by = request.user
        task.save(update_fields=["status", "approved_at", "approved_by"])
        earning = earnings_service.record_task_earning(task)

    if earning:
        log_activity(
            project, request.user,
            f"Approved “{task.title}” — ${earning.amount_usd:,.2f} released to "
            f"{task.assignee.full_name or task.assignee.email}."
        )
        payout_notifications.notify_task_earning_credited(task, earning.amount_usd)
    else:
        log_activity(project, request.user, f"Approved “{task.title}”.")
    notifications.notify_task_approved(task)
    return Response(TaskSerializer(task).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def request_task_changes(request, task_id):
    """Send a submitted task back, with a reason.

    The note isn't optional. "Changes requested" with nothing attached leaves
    the expert guessing at what stands between them and being paid.
    """
    task = _task_or_404(task_id)
    if not task:
        raise Http404
    if not leads_project(request.user, task.project):
        raise PermissionDenied("Only the delivery lead running this project can review a task.")
    if task.status != Task.Status.SUBMITTED:
        raise ValidationError("This task isn't waiting on review.")

    note = (request.data.get("note") or "").strip()
    if not note:
        raise ValidationError("Say what needs changing.")

    task.status = Task.Status.CHANGES
    task.submitted_at = None
    task.save(update_fields=["status", "submitted_at"])
    log_activity(task.project, request.user,
                 f"Requested changes on “{task.title}” — {note}")
    notifications.notify_task_changes_requested(task, note)
    return Response(TaskSerializer(task).data)


def _delete_task(request, task_id):
    """Drop a task from the list — with a reason, on the record.

    Refused once it has been paid: an earning protects its task at the database
    level, and answering with a clear reason beats a 500 from the ProtectedError
    underneath.

    The reason isn't optional. Removing a task changes what the client is
    getting and takes work — and money — off the person holding it. Both of
    them are owed an account of why, and the feed is where it lives afterwards.
    """
    task = _writable_task(request, task_id)
    if task.earnings.exists():
        raise ValidationError(
            "This task has been paid, so it stays on the record. "
            "Its amount is part of what the project has already paid out."
        )
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        raise ValidationError(
            "Say why this task is being removed — the client and whoever holds "
            "it are told, and the reason goes on the project record."
        )

    project, title, assignee = task.project, task.title, task.assignee
    amount = task.amount_usd
    task.delete()

    text = f"Removed “{title}” from the task list — {reason}"
    if amount > 0:
        text = (f"Removed “{title}” (${amount:,.2f}) from the task list — {reason}")
    log_activity(project, request.user, text)
    notifications.notify_task_removed(project, title, assignee, reason, amount)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_stats(request):
    if request.user.role != Role.DELIVERY_LEAD and not request.user.is_superuser:
        raise PermissionDenied("Delivery lead only.")
    qs = visible_projects(request.user)
    active = qs.exclude(stage=Stage.COMPLETED)
    by_line = {}
    for project in active.select_related("product_line"):
        if not project.product_line_id:
            continue
        row = by_line.setdefault(project.product_line.slug, {
            "slug": project.product_line.slug,
            "name": project.product_line.name,
            "accent": project.product_line.accent,
            "active": 0,
            "value_usd": 0,
        })
        row["active"] += 1
        row["value_usd"] += project.quote_usd
    # What this lead's own clients actually said. Not the public reviews —
    # this is every review on their projects, consented or not, which
    # `can_access_project` already permits them to read. It's the one signal
    # that measures whether anybody was happy rather than whether the work
    # arrived on time.
    feedback_rows = (ProjectFeedback.objects
                     .filter(project__in=qs)
                     .select_related("project")
                     .order_by("-created_at")[:5])
    ratings = list(ProjectFeedback.objects
                   .filter(project__in=qs)
                   .values_list("rating", flat=True))

    return Response({
        "active_total": active.count(),
        "needs_quote": qs.filter(stage=Stage.SUBMITTED).count(),
        "feedback": {
            # Null rather than zero with nothing to average — "nobody has rated
            # you" and "your clients rate you 0" are very different statements.
            "average": (round(sum(ratings) / len(ratings), 1)
                        if ratings else None),
            "count": len(ratings),
            "recent": [
                {
                    "id": row.id,
                    "project_id": row.project_id,
                    "project_title": row.project.title,
                    "company": row.project.company or "A client",
                    "rating": row.rating,
                    "comment": row.comment,
                    "would_work_again": row.would_work_again,
                    "may_publish": row.may_publish,
                    "created_at": row.created_at,
                }
                for row in feedback_rows
            ],
        },
        "needs_assign": qs.filter(stage=Stage.PAID, expert__isnull=True).count(),
        "contracted_value_usd": sum(p.quote_usd for p in active),
        # Per-discipline breakdown, busiest first.
        "by_line": sorted(by_line.values(), key=lambda r: -r["value_usd"]),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pipeline(request):
    """A business developer's book: referred clients, sourced projects, commission.

    The money figures come from the same ledger and projection the earnings page
    uses, so a BD's pipeline and their balance can never tell different stories.
    """
    user = request.user
    if user.role != Role.BUSINESS_DEV and not user.is_superuser:
        raise PermissionDenied("Business developers only.")

    from payments import earnings as earnings_service

    if not user.referral_code:
        user.ensure_referral_code()

    sourced = (Project.objects
               .filter(business_developer=user)
               .select_related("client", "product_line")
               .order_by("-created_at"))

    projects, won_value, open_value = [], 0, 0
    for project in sourced:
        split = project.payout_split()
        if project.stage == Stage.COMPLETED:
            won_value += project.quote_usd
        else:
            open_value += project.quote_usd
        projects.append({
            "id": project.id,
            "code": project.code,
            "title": project.title,
            "company": project.company,
            "client_name": project.client.full_name or project.client.email,
            "stage": project.stage,
            "quote_usd": project.quote_usd,
            "commission_usd": str(split["business_dev_usd"]),
            "commission_percent": str(split["business_dev_percent"]),
            "is_earned": project.stage == Stage.COMPLETED,
            "product_line": ({"name": project.product_line.name,
                              "slug": project.product_line.slug,
                              "accent": project.product_line.accent}
                             if project.product_line else None),
        })

    summary = earnings_service.summary(user)
    clients = (User.objects.filter(referred_by=user)
               .order_by("-date_joined")
               .values("id", "full_name", "email", "company"))

    return Response({
        "referral_code": user.referral_code,
        "clients_referred": len(clients),
        "clients": list(clients),
        "projects": projects,
        "projects_sourced": len(projects),
        "won_value_usd": won_value,
        "open_value_usd": open_value,
        # Commission actually earned vs still riding on live projects.
        "commission_earned_usd": str(summary["earned_usd"]),
        "commission_pending_usd": str(summary["pending_usd"]),
        "commission_available_usd": str(summary["available_usd"]),
        "commission_percent": str(summary["business_dev_share_percent"]),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_attachment(request, attachment_id):
    """Stream an uploaded project document to someone on the project."""
    attachment = (Attachment.objects
                  .select_related("project")
                  .filter(id=attachment_id)
                  .first())
    if not attachment or not attachment.file:
        raise Http404
    if not can_access_project(request.user, attachment.project):
        raise PermissionDenied("You can't view this document.")

    try:
        stream = attachment.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404

    response = FileResponse(
        stream, as_attachment=True,
        filename=attachment.original_filename or Path(attachment.file.name).name,
    )
    response["Cache-Control"] = "no-store, private"
    return response


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_attachment(request, attachment_id):
    """Remove a link.

    Whoever added it can take it back, and a delivery lead can tidy up anything
    on a project they run — but nobody else can remove another person's work
    from the record.
    """
    attachment = (Attachment.objects
                  .select_related("project", "project__client")
                  .filter(id=attachment_id)
                  .first())
    if not attachment:
        return Response({"detail": "Link not found."}, status=status.HTTP_404_NOT_FOUND)

    user = request.user
    if not (attachment.added_by_id == user.id
            or leads_project(user, attachment.project)):
        raise PermissionDenied("You can't remove this attachment.")
    # Drop the bytes as well as the row — otherwise deleted uploads sit on disk
    # forever, which for a client's confidential brief is worse than untidy.
    if attachment.file:
        attachment.file.delete(save=False)
    attachment.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def analytics(request):
    """What the platform itself has earned. Admins and staff only.

    Separate from `/api/reports` on purpose. Reports answer "how is my
    discipline doing?" and are scoped to whoever asks; this is the business's
    own books — every client, every line, and the margin on all of it — which
    is not a delivery lead's to read.
    """
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied("Analytics are for admins and staff.")

    from . import analytics as analytics_service

    return Response(analytics_service.dashboard())


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def reports(request):
    """Per-line P&L, the business-development leaderboard, and lead scorecards.

    Scoped exactly like the delivery board: a lead reports on the lines they
    run, a superuser on everything. The people leaderboards are platform-wide
    and admin-only — a lead ranking their peers isn't a thing this needs to do.
    """
    user = request.user
    if user.role != Role.DELIVERY_LEAD and not user.is_superuser:
        raise PermissionDenied("Delivery lead only.")
    if not user.is_approved:
        raise PermissionDenied("Your delivery lead account is still being reviewed.")

    from . import reports as reporting

    scope = visible_projects(user)
    payload = {
        "totals": reporting.totals(scope),
        "product_lines": reporting.product_lines(scope),
        "is_platform_wide": bool(user.is_superuser),
    }
    if user.is_superuser:
        payload["business_developers"] = reporting.business_developers()
        payload["delivery_leads"] = reporting.delivery_leads()
        # The reserve sits next to `in_flight_paid_usd` on purpose: one is the
        # exposure, the other is what's set aside against it, and reading
        # either without the other tells you half the story. Admin-only — it's
        # a platform-wide pot, not a per-line figure a lead can act on.
        from payments import refunds as refund_service
        from payments.models import Refund

        settled = Refund.objects.filter(status=Refund.Status.PROCESSED)
        payload["reserve"] = {
            "balance_usd": str(refund_service.reserve_balance()),
            "refunded_usd": str(sum((r.amount_usd for r in settled), Decimal("0"))),
            "absorbed_usd": str(sum((r.absorbed_usd for r in settled), Decimal("0"))),
            "pending_count": Refund.objects.filter(
                status=Refund.Status.REQUESTED).count(),
        }
    return Response(payload)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def withdraw_change_order(request, order_id):
    """Take back a change order the client hasn't paid yet.

    Only while unpaid. A paid one is contract value the pool has already been
    grown by and tasks may already be priced against — unwinding that is a
    refund, not a deletion.
    """
    order = (ChangeOrder.objects
             .select_related("project").filter(id=order_id).first())
    if not order:
        return Response({"detail": "No change order with that id."},
                        status=status.HTTP_404_NOT_FOUND)
    if not (leads_project(request.user, order.project) or request.user.is_superuser):
        raise PermissionDenied("Only this project's delivery lead can withdraw it.")
    if order.status == ChangeOrder.Status.PAID:
        raise ValidationError(
            "The client has already paid for this. Refund it instead."
        )
    order.status = ChangeOrder.Status.WITHDRAWN
    order.save(update_fields=["status"])
    log_activity(order.project, request.user,
                 f"Withdrew the ${order.amount_usd:,.2f} change order.")
    return Response(ChangeOrderSerializer(order).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def utilisation(request):
    """How much of an expert's available time has had paid work on it.

    Three audiences, three scopes, one endpoint:

    * an **expert** sees their own — this is the retention surface, and the
      honest answer to "am I better off here than bidding for myself?";
    * a **delivery lead** sees their roster, worst first, so idle capacity
      surfaces before somebody quits over it;
    * an **admin** additionally gets the platform figure, which is the number
      to recruit on.
    """
    from . import utilisation as util

    user = request.user
    start, end = util.default_window()
    if user.role == Role.EXPERT:
        return Response({"start": str(start), "end": str(end),
                         "me": util.for_expert(user, start, end)})
    if user.role == Role.DELIVERY_LEAD or user.is_superuser:
        payload = util.for_roster(user, start, end)
        if user.is_superuser:
            payload["platform"] = util.platform(start, end)
        return Response(payload)
    raise PermissionDenied("Utilisation is for experts and delivery leads.")


# ---------------------------------------------------------------------------
# Engagements — retainers billed monthly
# ---------------------------------------------------------------------------

def _visible_engagements(user):
    """Whose retainers this person can see, mirroring project scope.

    A lead sees the ones they run; a client sees their company's; an expert
    sees the ones they're actually delivering, found through the cycles rather
    than a second membership list that could drift from the first.
    """
    base = Engagement.objects.select_related(
        "organisation", "client", "lead", "product_line", "service"
    ).prefetch_related("cycles")
    if user.is_superuser:
        return base
    if user.role == Role.DELIVERY_LEAD:
        return base.filter(lead=user)
    if user.role == Role.EXPERT:
        return base.filter(
            Q(cycles__experts=user) | Q(cycles__expert=user)).distinct()
    orgs = user.organisation_memberships.values_list("organisation_id", flat=True)
    return base.filter(
        Q(client=user) | Q(organisation__in=list(orgs))).distinct()


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def engagements(request):
    """List retainers, or (delivery lead) set one up.

    A lead creates these rather than a client, for the same reason a lead
    quotes a brief: the price and the scope of an ongoing seat are negotiated,
    not self-served.
    """
    if request.method == "GET":
        return Response(EngagementSerializer(
            _visible_engagements(request.user), many=True).data)

    if request.user.role != Role.DELIVERY_LEAD and not request.user.is_superuser:
        raise PermissionDenied("Only a delivery lead can set up a retainer.")
    if not request.user.is_approved:
        raise PermissionDenied("Your delivery lead account is still being reviewed.")

    serializer = EngagementCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    client = serializer.validated_data["client"]
    if client.role != Role.CLIENT:
        raise ValidationError("A retainer is bought by a client.")
    seat = client.organisation_memberships.select_related("organisation").first()
    if not seat:
        raise ValidationError(
            "That client isn't attached to a company yet, and a retainer bills "
            "to one."
        )
    engagement = serializer.save(lead=request.user, organisation=seat.organisation)
    return Response(EngagementSerializer(engagement).data,
                    status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def engagement_detail(request, engagement_id):
    """Read one retainer, or act on it: pause, resume, end, or raise a cycle."""
    engagement = _visible_engagements(request.user).filter(id=engagement_id).first()
    if not engagement:
        return Response({"detail": "No retainer with that id."},
                        status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(EngagementSerializer(engagement).data)

    runs_it = (request.user.is_superuser
               or (request.user.role == Role.DELIVERY_LEAD
                   and engagement.lead_id == request.user.id))
    if not runs_it:
        raise PermissionDenied("Only this retainer's delivery lead can change it.")

    action_name = (request.data.get("action") or "").strip()
    if action_name == "pause":
        engagement.status = Engagement.Status.PAUSED
        engagement.save(update_fields=["status"])
    elif action_name == "resume":
        if engagement.status == Engagement.Status.ENDED:
            raise ValidationError("An ended retainer can't be resumed — set up a new one.")
        engagement.status = Engagement.Status.ACTIVE
        engagement.save(update_fields=["status"])
    elif action_name == "end":
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            raise ValidationError("Say why the retainer is ending.")
        engagement.status = Engagement.Status.ENDED
        engagement.ended_at = timezone.now()
        engagement.end_reason = reason
        engagement.save(update_fields=["status", "ended_at", "end_reason"])
        # A month already paid for is delivered. Ending stops future billing;
        # it does not cancel work the client has settled.
        latest = engagement.cycles.order_by("-period_start").first()
        notifications.notify_engagement_ended(engagement, final_cycle=latest)
    elif action_name == "raise-cycle":
        # The manual path, for a retainer set up mid-month that shouldn't wait
        # for the scheduler. It skips only the lead-time window — every other
        # guard still applies, or a lead clicking twice would bill a client
        # months in advance.
        from . import engagements as service

        if not engagement.is_live:
            raise ValidationError(
                f"This retainer is {engagement.get_status_display().lower()}.")
        blocked = service.blocking_cycle(engagement)
        if blocked:
            raise ValidationError(
                f"{blocked.period_start:%B}'s cycle hasn't been paid yet. "
                "Chase that before raising another."
            )
        cycle = service.generate_cycle(engagement)
        if cycle is None:
            raise ValidationError("That period has already been raised.")
        service._announce(cycle)
    else:
        raise ValidationError("Pick an action: pause, resume, end, or raise-cycle.")

    engagement.refresh_from_db()
    return Response(EngagementSerializer(engagement).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def reviews(request):
    """Consented client reviews, plus the honest aggregate.

    Public on purpose — this is the landing page's social proof, and a visitor
    deciding whether to trust the platform hasn't signed in yet.

    Only reviews whose author agreed we may quote them. The average beside them
    is computed over *every* review, consented or not, because a wall of
    testimonials is understood to be curated and a rating is not.
    """
    from . import reviews as service

    line = request.query_params.get("product_line")
    return Response({
        "reviews": service.published(
            limit=int(request.query_params.get("limit") or 12),
            line_slug=line),
        "summary": service.summary(line_slug=line),
    })
