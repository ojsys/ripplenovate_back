from rest_framework import serializers

from catalog.models import ProductLine, Service
from catalog.serializers import ProductLineBriefSerializer

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


def _initials(name):
    parts = (name or "").split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (name or "?")[:2].upper()


class TaskSerializer(serializers.ModelSerializer):
    
    assignee_name = serializers.SerializerMethodField()
    # `done` is now a view of `status`, kept so existing boards keep reading the
    # same field while the lifecycle grows underneath them.
    done = serializers.BooleanField(read_only=True)

    class Meta:
        model = Task
        fields = ["id", "title", "description", "done", "status", "amount_usd",
                  "order", "assignee", "assignee_name", "submitted_at",
                  "approved_at"]
        read_only_fields = ["assignee", "assignee_name", "status", "amount_usd",
                            "submitted_at", "approved_at"]

    def get_assignee_name(self, obj):
        return obj.assignee.full_name if obj.assignee else ""


class TaskWriteSerializer(serializers.ModelSerializer):
    """A delivery lead creating a task.

    `status` is absent on purpose. It moves through the lifecycle actions —
    submit, approve, request changes — which check who is asking and release
    money on the way. A writable status field here would be a way to pay
    someone with a PATCH.
    """

    class Meta:
        model = Task
        fields = ["title", "description", "assignee", "amount_usd", "order"]

    def validate_title(self, value):
        if not value.strip():
            raise serializers.ValidationError("Give the task a title.")
        return value.strip()

    def validate_amount_usd(self, value):
        if value < 0:
            raise serializers.ValidationError("An amount can't be negative.")
        return value

    def validate_assignee(self, value):
        if value and value.role != value.Role.EXPERT:
            raise serializers.ValidationError("Tasks are assigned to experts.")
        return value


class TaskEditSerializer(TaskWriteSerializer):
    """Editing an existing task. Everything except who holds it.

    Moving a task between people is its own action: it has different rules —
    it's allowed on work already handed in, where re-pricing isn't — and it owes
    two people an explanation rather than one. Leaving `assignee` here as well
    would give it two doors with two sets of rules.
    """

    class Meta(TaskWriteSerializer.Meta):
        fields = ["title", "description", "amount_usd", "order"]


class TaskReassignSerializer(serializers.Serializer):
    """Hand a task to someone else on the team, or take it off everyone."""

    assignee = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="An expert on this project's team, or null to unassign.",
    )
    note = serializers.CharField(required=False, allow_blank=True)


class AttachmentSerializer(serializers.ModelSerializer):
    added_by_name = serializers.SerializerMethodField()
    is_file = serializers.BooleanField(read_only=True)

    class Meta:
        model = Attachment
        fields = ["id", "url", "label", "kind", "purpose", "created_at",
                  "added_by", "added_by_name", "is_file", "original_filename",
                  "size_bytes"]
        read_only_fields = ["kind", "added_by", "created_at", "is_file",
                            "original_filename", "size_bytes"]

    def get_added_by_name(self, obj):
        if not obj.added_by:
            return ""
        return obj.added_by.full_name or obj.added_by.email


class AttachmentCreateSerializer(serializers.Serializer):
    """One link. The kind is detected from the URL rather than asked for."""

    url = serializers.URLField(max_length=1000)
    label = serializers.CharField(max_length=200, required=False, allow_blank=True)
    purpose = serializers.ChoiceField(
        choices=Attachment.Purpose.choices,
        default=Attachment.Purpose.DELIVERABLE,
    )


class RevisionRequestSerializer(serializers.ModelSerializer):
    requested_by_name = serializers.SerializerMethodField()

    class Meta:
        model = RevisionRequest
        fields = ["id", "note", "created_at", "resolved_at", "requested_by_name"]
        read_only_fields = fields

    def get_requested_by_name(self, obj):
        if not obj.requested_by:
            return ""
        return obj.requested_by.full_name or obj.requested_by.email


class ChangeOrderSerializer(serializers.ModelSerializer):
    raised_by_name = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    is_payable = serializers.BooleanField(read_only=True)

    class Meta:
        model = ChangeOrder
        fields = ["id", "description", "amount_usd", "status", "status_label",
                  "is_payable", "raised_by_name", "created_at", "paid_at"]
        read_only_fields = fields

    def get_raised_by_name(self, obj):
        if not obj.raised_by:
            return ""
        return obj.raised_by.full_name or obj.raised_by.email


class ChangeOrderCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChangeOrder
        fields = ["description", "amount_usd"]

    def validate_description(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError(
                "Describe the extra work — the client sees this on the invoice."
            )
        return value

    def validate_amount_usd(self, value):
        if value <= 0:
            raise serializers.ValidationError("A change order has to be worth something.")
        return value


class ProjectFeedbackSerializer(serializers.ModelSerializer):
    """The client writing their verdict, and the lead reading it.

    Same serializer both ways — there is nothing in here the client shouldn't
    see, because they wrote it. Who may *read* it is enforced on the project
    serializer, not by narrowing fields here.
    """

    class Meta:
        model = ProjectFeedback
        fields = ["rating", "comment", "would_work_again", "may_publish",
                  "created_at"]
        read_only_fields = ["created_at"]

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rate it from 1 to 5.")
        return value


class ActivityReplySerializer(serializers.ModelSerializer):
    """A reply. Flat by construction — a reply never has replies of its own."""

    initials = serializers.SerializerMethodField()
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "author_name", "role_label", "text", "created_at",
                  "initials", "attachments"]
        read_only_fields = fields

    def get_initials(self, obj):
        return _initials(obj.author_name)


class ActivitySerializer(serializers.ModelSerializer):
    initials = serializers.SerializerMethodField()
    attachments = AttachmentSerializer(many=True, read_only=True)
    replies = ActivityReplySerializer(many=True, read_only=True)
    # The deliverable this comment is about, if any. Just the id — the file's
    # own details are already in the attachments list the page renders.
    attachment = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "author_name", "role_label", "kind", "text", "created_at",
                  "initials", "attachments", "replies", "attachment"]

    def get_initials(self, obj):
        return _initials(obj.author_name)


class ProjectListSerializer(serializers.ModelSerializer):
    expert_name = serializers.SerializerMethodField()
    product_line = ProductLineBriefSerializer(read_only=True)
    service_name = serializers.SerializerMethodField()
    progress_pct = serializers.IntegerField(read_only=True)
    is_paid = serializers.BooleanField(read_only=True)
    stage_index = serializers.IntegerField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    days_overdue = serializers.IntegerField(read_only=True, allow_null=True)
    is_on_time = serializers.BooleanField(read_only=True, allow_null=True)
    days_late = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = Project
        fields = [
            "id", "code", "title", "company", "category", "stage", "stage_index",
            "quote_usd", "expert", "expert_name", "progress_pct", "is_paid",
            "target_date", "created_at", "product_line", "service_name",
            "is_overdue", "days_overdue", "is_on_time", "days_late",
            "revision_rounds",
        ]
        # Output only. Lifecycle fields are moved by the viewset's actions (which
        # log activity and notify), never written straight from a request body.
        read_only_fields = [
            "code", "title", "company", "category", "stage", "quote_usd",
            "expert", "target_date", "created_at", "product_line", "service_name",
            "is_overdue", "days_overdue", "is_on_time", "days_late",
            "revision_rounds",
        ]

    def get_service_name(self, obj):
        return obj.service.name if obj.service else obj.category

    def get_expert_name(self, obj):
        return obj.expert.full_name if obj.expert else ""


class TeamMemberSerializer(serializers.Serializer):
    """One expert on a project's delivery team."""

    id = serializers.IntegerField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True)
    specialty = serializers.CharField(read_only=True)
    initials = serializers.SerializerMethodField()
    is_primary = serializers.SerializerMethodField()

    def get_initials(self, obj):
        return _initials(obj.full_name or obj.email)

    def get_is_primary(self, obj):
        return obj.id == self.context.get("primary_expert_id")


class ProjectDetailSerializer(ProjectListSerializer):
    client_name = serializers.SerializerMethodField()
    expert_role = serializers.SerializerMethodField()
    business_developer_name = serializers.SerializerMethodField()
    team = serializers.SerializerMethodField()
    # All three go through methods so a billing-only seat can be handed the
    # money and not the work. Activity is roots-only besides: replies are
    # carried nested inside their parent, so listing everything flat here
    # would render each one twice.
    tasks = serializers.SerializerMethodField()
    activity = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    # What the lead has to hand out, and what's left. The allocation meter is
    # where they find out they've committed more than the project holds — it
    # should say so before a save fails.
    expert_pool_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    allocated_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    unallocated_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    uses_task_payouts = serializers.BooleanField(read_only=True)
    # The change request the team still owes an answer to. Carried in full
    # rather than as a flag: the client needs to see what they asked for while
    # they wait, and the team needs it in front of them while they fix it.
    open_revision = serializers.SerializerMethodField()
    revision_history = serializers.SerializerMethodField()
    # Why a lead can't close this over the client's head yet, or null when they
    # can. Sent so the board can explain the rule in place rather than letting
    # someone discover it by getting a 403.
    completion_block = serializers.SerializerMethodField()
    # The refund position, so a lead cancelling a paid project can see what's
    # actually returnable rather than guessing from the quote.
    collected_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    refunded_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    refundable_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    free_refund_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    # Extra scope, and what the project is now worth in total. Kept apart from
    # `quote_usd` so the invoice the client settled stays legible next to it.
    change_orders = ChangeOrderSerializer(many=True, read_only=True)
    change_orders_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    contract_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    # The client's private verdict. Visible to whoever wrote it, the lead
    # running the project, and admins — never to the experts who delivered it,
    # and never to another lead. Withheld here rather than filtered in the view
    # so there's one place the rule lives.
    feedback = serializers.SerializerMethodField()
    can_leave_feedback = serializers.SerializerMethodField()

    class Meta(ProjectListSerializer.Meta):
        fields = ProjectListSerializer.Meta.fields + [
            "client_name", "description", "timeline", "budget_range",
            "expert_role", "team", "tasks", "activity", "attachments",
            "business_developer", "business_developer_name",
            "expert_pool_usd", "allocated_usd", "unallocated_usd",
            "uses_task_payouts", "open_revision", "revision_history",
            "completion_block", "cancelled_at", "cancellation_reason",
            "collected_usd", "refunded_usd", "refundable_usd", "free_refund_usd",
            "change_orders", "change_orders_usd", "contract_usd",
            "feedback", "can_leave_feedback",
        ]

    def _viewer(self):
        request = self.context.get("request")
        return getattr(request, "user", None)

    def get_feedback(self, obj):
        entry = getattr(obj, "feedback", None)
        if entry is None:
            return None
        viewer = self._viewer()
        if viewer is None:
            return None
        may_read = (
            viewer.id == obj.client_id
            or viewer.id == obj.lead_id
            or viewer.is_superuser
        )
        return ProjectFeedbackSerializer(entry).data if may_read else None

    def get_can_leave_feedback(self, obj):
        """Whether the person reading this is the client, and still owes one."""
        viewer = self._viewer()
        if viewer is None or viewer.id != obj.client_id:
            return False
        return (obj.stage in Project.CLOSED_STAGES
                and getattr(obj, "feedback", None) is None)

    def _sees_delivery(self, obj):
        """Whether the reader is entitled to the work, or only to the invoice.

        Redacted in the serializer rather than the view so there is one place
        the rule lives — a billing-only seat that could reach the deliverables
        through some other endpoint would make the distinction decorative.
        """
        from .access import can_see_delivery

        viewer = self._viewer()
        return viewer is None or can_see_delivery(viewer, obj)

    def get_activity(self, obj):
        if not self._sees_delivery(obj):
            return []
        roots = [a for a in obj.activity.all() if a.parent_id is None]
        return ActivitySerializer(roots, many=True).data

    def get_attachments(self, obj):
        if not self._sees_delivery(obj):
            return []
        return AttachmentSerializer(obj.attachments.all(), many=True).data

    def get_tasks(self, obj):
        if not self._sees_delivery(obj):
            return []
        return TaskSerializer(obj.tasks.all(), many=True).data

    def get_completion_block(self, obj):
        if obj.stage != Project.Stage.REVIEW:
            return None
        return obj.client_silence_block()

    def get_open_revision(self, obj):
        return RevisionRequestSerializer(obj.open_revision).data if obj.open_revision else None

    def get_revision_history(self, obj):
        return RevisionRequestSerializer(
            obj.revision_requests.all(), many=True).data

    def get_team(self, obj):
        # Ordered so the primary expert reads first, then by name.
        members = sorted(
            obj.experts.all(),
            key=lambda u: (u.id != obj.expert_id, (u.full_name or u.email).lower()),
        )
        return TeamMemberSerializer(
            members, many=True, context={"primary_expert_id": obj.expert_id}
        ).data
        read_only_fields = ProjectListSerializer.Meta.read_only_fields + [
            "description", "timeline", "budget_range", "business_developer",
        ]

    def get_client_name(self, obj):
        return obj.client.full_name or obj.client.email

    def get_expert_role(self, obj):
        return obj.expert.specialty if obj.expert else ""

    def get_business_developer_name(self, obj):
        bd = obj.business_developer
        return (bd.full_name or bd.email) if bd else ""


class ProjectCreateSerializer(serializers.ModelSerializer):
    """A client posting a brief.

    The line and service are what route the brief to the right delivery leads,
    so they're validated against the live catalogue rather than accepted as
    free text. `category` is derived from the chosen service and snapshotted, so
    an invoice still reads correctly if the catalogue is reorganised later.
    """

    product_line = serializers.SlugRelatedField(
        slug_field="slug", queryset=ProductLine.objects.filter(is_active=True)
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.filter(is_active=True), required=False, allow_null=True
    )

    # Brand assets, existing research, examples to work from. Optional, but the
    # single most useful thing a client can give a designer up front.
    references = AttachmentCreateSerializer(many=True, required=False, default=list)

    class Meta:
        model = Project
        fields = ["title", "product_line", "service", "timeline",
                  "budget_range", "description", "references"]

    def validate_title(self, v):
        if not v.strip():
            raise serializers.ValidationError("Add a project title.")
        return v.strip()

    def validate_description(self, v):
        if not v.strip():
            raise serializers.ValidationError("Describe what you need delivered.")
        return v.strip()

    def validate(self, attrs):
        service, line = attrs.get("service"), attrs.get("product_line")
        if service and line and service.product_line_id != line.id:
            raise serializers.ValidationError(
                {"service": "That service isn't part of the selected product line."}
            )
        # Snapshot the human-readable service name onto the project.
        attrs["category"] = service.name if service else (line.name if line else "")
        return attrs


class ProjectEditSerializer(serializers.ModelSerializer):
    """A delivery lead correcting a brief, or re-pricing a quote.

    Only the fields a lead legitimately maintains. `stage`, `expert` and
    `client` are absent on purpose — those move through the lifecycle actions,
    which log activity and notify the people involved.
    """

    class Meta:
        model = Project
        fields = ["title", "category", "timeline", "budget_range", "description",
                  "target_date", "quote_usd"]
        extra_kwargs = {field: {"required": False} for field in fields}
        # Clearing the date is legitimate — it means "not agreed yet" again.
        extra_kwargs["target_date"] = {"required": False, "allow_null": True}

    def validate_title(self, v):
        if not v.strip():
            raise serializers.ValidationError("The project needs a title.")
        return v.strip()

    def validate_description(self, v):
        if not v.strip():
            raise serializers.ValidationError("The brief can't be empty.")
        return v.strip()

    def validate_quote_usd(self, v):
        if v < 1:
            raise serializers.ValidationError("A quote has to be at least $1.")
        return v

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Nothing to change.")
        return attrs


class QuoteSerializer(serializers.Serializer):
    quote_usd = serializers.IntegerField(min_value=1)


class ExpertListSerializer(serializers.Serializer):
    """One or more experts, however the caller chose to say it.

    `expert` (singular) is the original shape and still what the assign screen
    sends; `experts` is the list. Normalising both here means every caller
    downstream sees the same thing, and the older clients keep working.
    """

    expert = serializers.IntegerField(required=False)
    experts = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )

    def validate(self, attrs):
        ids = list(attrs.get("experts") or [])
        single = attrs.get("expert")
        if single and single not in ids:
            # First in the list is the primary, so a singular `expert` leads.
            ids.insert(0, single)
        if not ids:
            raise serializers.ValidationError("Choose at least one expert.")
        # Preserve order, drop repeats — the first mention wins.
        attrs["expert_ids"] = list(dict.fromkeys(ids))
        return attrs


class AssignSerializer(ExpertListSerializer):
    tasks = serializers.ListField(
        child=serializers.CharField(allow_blank=True), required=False, default=list
    )


class ActivityCreateSerializer(serializers.Serializer):
    text = serializers.CharField()
    kind = serializers.ChoiceField(
        choices=[k for k in Activity.POSTABLE_KINDS],
        default=Activity.Kind.UPDATE,
    )
    # Links shared with the update — how design and research work is handed over.
    attachments = AttachmentCreateSerializer(many=True, required=False, default=list)
    # What this replies to, and what it's about. Both optional and both
    # validated against the project in the view rather than here — the
    # serializer has no idea which project it's writing to.
    parent = serializers.IntegerField(required=False, allow_null=True)
    attachment = serializers.IntegerField(required=False, allow_null=True)


class EngagementCycleSerializer(serializers.ModelSerializer):
    """One month of a retainer, as the client and lead see it in a list."""

    class Meta:
        model = Project
        fields = ["id", "code", "title", "stage", "quote_usd", "is_paid",
                  "period_start", "period_end", "progress_pct"]
        read_only_fields = fields


class EngagementSerializer(serializers.ModelSerializer):
    organisation_name = serializers.CharField(
        source="organisation.name", read_only=True)
    client_name = serializers.SerializerMethodField()
    lead_name = serializers.SerializerMethodField()
    product_line = ProductLineBriefSerializer(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    cycles = serializers.SerializerMethodField()
    delivered_cycles = serializers.IntegerField(read_only=True)
    billed_usd = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    next_period_start = serializers.SerializerMethodField()

    class Meta:
        model = Engagement
        fields = ["id", "title", "description", "monthly_amount_usd",
                  "billing_day", "status", "status_label", "started_on",
                  "ends_on", "end_reason", "organisation", "organisation_name",
                  "client_name", "lead", "lead_name", "product_line",
                  "cycles", "delivered_cycles", "billed_usd",
                  "next_period_start", "created_at"]
        read_only_fields = fields

    def get_client_name(self, obj):
        return obj.client.full_name or obj.client.email

    def get_lead_name(self, obj):
        return obj.lead.full_name or obj.lead.email

    def get_cycles(self, obj):
        return EngagementCycleSerializer(
            obj.cycles.order_by("-period_start"), many=True).data

    def get_next_period_start(self, obj):
        """What the client would be billed for next, or null if nothing is due.

        Shown so a retainer never quietly stops without anybody noticing —
        "next: 1 October" and "next: nothing" are very different states.
        """
        from . import engagements as service

        if not obj.is_live:
            return None
        return service.next_period_start(obj)


class EngagementCreateSerializer(serializers.ModelSerializer):
    product_line = serializers.SlugRelatedField(
        slug_field="slug", queryset=ProductLine.objects.filter(is_active=True))
    service = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.filter(is_active=True),
        required=False, allow_null=True)

    class Meta:
        model = Engagement
        fields = ["title", "description", "monthly_amount_usd", "billing_day",
                  "started_on", "ends_on", "product_line", "service", "client"]

    def validate_title(self, value):
        if not value.strip():
            raise serializers.ValidationError("Give the retainer a name.")
        return value.strip()

    def validate_monthly_amount_usd(self, value):
        if value <= 0:
            raise serializers.ValidationError("A retainer has to be worth something.")
        return value

    def validate(self, data):
        ends = data.get("ends_on")
        if ends and ends < data["started_on"]:
            raise serializers.ValidationError(
                {"ends_on": "It can't end before it starts."})
        return data
