import logging

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.http import FileResponse, Http404
from django.utils import timezone

from accounts.uploads import validate_project_document
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import notifications
from .models import Activity, Attachment, Project, Task
from .serializers import (
    ActivityCreateSerializer,
    AssignSerializer,
    AttachmentCreateSerializer,
    AttachmentSerializer,
    ProjectCreateSerializer,
    ProjectDetailSerializer,
    ProjectEditSerializer,
    ProjectListSerializer,
    QuoteSerializer,
    TaskSerializer,
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
        ).prefetch_related("tasks", "activity", "activity__attachments", "attachments")
        if user.is_superuser:
            return base
        if user.role == Role.DELIVERY_LEAD:
            # A lead sees the briefs in the disciplines they run, plus anything
            # they already lead — never the whole platform. Without the second
            # clause a lead would lose sight of a project the moment they (or an
            # admin) changed which lines they cover.
            lines = user.product_lines.values_list("id", flat=True)
            return base.filter(
                Q(product_line__in=lines) | Q(lead=user)
            ).distinct()
        if user.role == Role.EXPERT:
            return base.filter(expert=user)
        return base.filter(client=user)

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
            "client", "expert", "product_line", "service"
        ).prefetch_related(
            "tasks", "activity", "activity__attachments", "attachments"
        ).get(pk=project.pk)
        return Response(ProjectDetailSerializer(fresh).data, status=status_code)

    def create(self, request, *args, **kwargs):
        if request.user.role != Role.CLIENT:
            raise PermissionDenied("Only clients can post projects.")
        serializer = ProjectCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        references = serializer.validated_data.pop("references", [])
        project = serializer.save(
            client=request.user,
            company=request.user.company,
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
                   or (user.role == Role.EXPERT and project.expert_id == user.id))
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

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        project = self.get_object()
        self._require_lead()
        if project.stage != Stage.PAID:
            raise ValidationError("An expert can only be assigned after payment.")
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expert = User.objects.filter(
            id=serializer.validated_data["expert"], role=Role.EXPERT
        ).first()
        if not expert:
            raise ValidationError("Select a valid expert.")
        # An expert can only take work in a discipline they actually cover. The
        # picker already filters to these, so this is the server-side backstop.
        if (project.product_line_id
                and not expert.product_lines.filter(id=project.product_line_id).exists()):
            raise ValidationError(
                f"{expert.full_name or expert.email} doesn't work in "
                f"{project.product_line.name}. Add that product line to their "
                "profile first, or pick someone else."
            )
        titles = [t.strip() for t in serializer.validated_data.get("tasks", []) if t.strip()]
        project.expert = expert
        project.stage = Stage.IN_PROGRESS
        # Covers briefs that were paid before leads were tracked on the project.
        if not project.lead_id:
            project.lead = request.user
        project.save(update_fields=["expert", "stage", "lead"])
        if titles:
            project.tasks.all().delete()
            Task.objects.bulk_create([
                Task(project=project, title=t, assignee=expert, order=i)
                for i, t in enumerate(titles)
            ])
        log_activity(project, request.user,
                     f"Assigned {expert.full_name} and kicked off delivery.")
        notifications.notify_expert_assigned(project)
        return self._detail(project)

    def _is_lead(self):
        user = self.request.user
        return user.role == Role.DELIVERY_LEAD or user.is_superuser

    @action(detail=True, methods=["post"], url_path="submit-review")
    def submit_review(self, request, pk=None):
        """Hand the work to the client. The assigned expert or the lead can do it —
        the lead often needs to move it along on the expert's behalf."""
        project = self.get_object()
        is_assigned_expert = (request.user.role == Role.EXPERT
                           and project.expert_id == request.user.id)
        if not (is_assigned_expert or self._is_lead()):
            raise PermissionDenied(
                "Only the assigned expert or a delivery lead can submit for review."
            )
        if project.stage != Stage.IN_PROGRESS:
            raise ValidationError("This project isn't in progress.")
        project.stage = Stage.REVIEW
        project.save(update_fields=["stage"])
        text = ("Submitted the work for client review."
                if is_assigned_expert else
                "Moved the project to review and notified the client.")
        log_activity(project, request.user, text)
        notifications.notify_submitted_for_review(project)
        return self._detail(project)

    @action(detail=True, methods=["post"], url_path="remind-review")
    def remind_review(self, request, pk=None):
        """Nudge the client again while a project is sitting in review."""
        project = self.get_object()
        is_assigned_expert = (request.user.role == Role.EXPERT
                           and project.expert_id == request.user.id)
        if not (is_assigned_expert or self._is_lead()):
            raise PermissionDenied("Only the delivery team can send that reminder.")
        if project.stage != Stage.REVIEW:
            raise ValidationError("This project isn't waiting on the client's review.")
        log_activity(project, request.user,
                     "Reminded the client that the work is ready for review.")
        notifications.notify_review_reminder(project)
        return self._detail(project)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Close the project out and release the earnings.

        The client signs their own work off; a delivery lead can also complete it
        — needed when a client goes quiet and the team is owed their share.
        """
        project = self.get_object()
        is_client = (request.user.role == Role.CLIENT
                     and project.client_id == request.user.id)
        if not (is_client or self._is_lead()):
            raise PermissionDenied("Only the client or a delivery lead can complete this.")
        if project.stage != Stage.REVIEW:
            raise ValidationError("This project isn't ready for approval.")
        project.stage = Stage.COMPLETED
        project.completed_at = timezone.now()
        project.save(update_fields=["stage", "completed_at"])
        text = ("Approved delivery. Project complete!"
                if is_client else
                "Marked the project complete on the client's behalf.")
        log_activity(project, request.user, text)
        notifications.notify_project_completed(project, completed_by_client=is_client)
        credit_earnings(project)
        return self._detail(project)

    @action(detail=True, methods=["post"])
    def activity(self, request, pk=None):
        project = self.get_object()
        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        text = serializer.validated_data["text"].strip()
        if not text:
            raise ValidationError("Write an update first.")
        entry = log_activity(project, request.user, text, kind=serializer.validated_data["kind"])
        for link in serializer.validated_data.get("attachments", []):
            Attachment.objects.create(
                project=project, activity=entry, url=link["url"],
                label=link.get("label", ""), purpose=link.get("purpose"),
                added_by=request.user,
            )
        notifications.notify_update_posted(project, entry)
        return self._detail(project)

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
                   or (user.role == Role.EXPERT and project.expert_id == user.id))
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


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def toggle_task(request, task_id):
    task = Task.objects.select_related("project", "project__expert",
                                       "project__client").filter(id=task_id).first()
    if not task:
        return Response({"detail": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
    project = task.project
    user = request.user
    allowed = (
        user.is_superuser
        or user.role == Role.DELIVERY_LEAD
        or (user.role == Role.EXPERT and project.expert_id == user.id)
    )
    if not allowed:
        raise PermissionDenied("You can't change tasks on this project.")
    task.done = not task.done
    task.save(update_fields=["done"])
    return Response(TaskSerializer(task).data)


def visible_projects(user):
    """The projects a delivery lead's board covers — their lines, plus their own.

    Mirrors ProjectViewSet.get_queryset so the stat tiles can never disagree
    with the table underneath them.
    """
    base = Project.objects.all()
    if user.is_superuser:
        return base
    lines = user.product_lines.values_list("id", flat=True)
    return base.filter(Q(product_line__in=lines) | Q(lead=user)).distinct()


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
    return Response({
        "active_total": active.count(),
        "needs_quote": qs.filter(stage=Stage.SUBMITTED).count(),
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


def _on_project(user, project):
    """Everyone attached to a project may read its documents.

    The client who commissioned it, the expert delivering it, the lead running
    it, and admins. Nobody else — a brief can contain anything from budgets to
    unreleased plans.
    """
    if user.is_superuser or user.role == Role.DELIVERY_LEAD:
        return True
    return user.id in (project.client_id, project.expert_id, project.lead_id,
                       project.business_developer_id)


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
    if not _on_project(request.user, attachment.project):
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
    is_lead = user.role == Role.DELIVERY_LEAD or user.is_superuser
    if not (attachment.added_by_id == user.id or is_lead):
        raise PermissionDenied("You can't remove this attachment.")
    # Drop the bytes as well as the row — otherwise deleted uploads sit on disk
    # forever, which for a client's confidential brief is worse than untidy.
    if attachment.file:
        attachment.file.delete(save=False)
    attachment.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


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
    return Response(payload)
