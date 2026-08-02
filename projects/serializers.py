from rest_framework import serializers

from catalog.models import ProductLine, Service
from catalog.serializers import ProductLineBriefSerializer

from .models import Activity, Attachment, Project, Task


def _initials(name):
    parts = (name or "").split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (name or "?")[:2].upper()


class TaskSerializer(serializers.ModelSerializer):
    assignee_name = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = ["id", "title", "done", "order", "assignee", "assignee_name"]
        read_only_fields = ["assignee", "assignee_name"]

    def get_assignee_name(self, obj):
        return obj.assignee.full_name if obj.assignee else ""


class AttachmentSerializer(serializers.ModelSerializer):
    added_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = ["id", "url", "label", "kind", "purpose", "created_at",
                  "added_by", "added_by_name"]
        read_only_fields = ["kind", "added_by", "created_at"]

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


class ActivitySerializer(serializers.ModelSerializer):
    initials = serializers.SerializerMethodField()
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "author_name", "role_label", "kind", "text", "created_at",
                  "initials", "attachments"]

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
        ]
        # Output only. Lifecycle fields are moved by the viewset's actions (which
        # log activity and notify), never written straight from a request body.
        read_only_fields = [
            "code", "title", "company", "category", "stage", "quote_usd",
            "expert", "target_date", "created_at", "product_line", "service_name",
            "is_overdue", "days_overdue", "is_on_time", "days_late",
        ]

    def get_service_name(self, obj):
        return obj.service.name if obj.service else obj.category

    def get_expert_name(self, obj):
        return obj.expert.full_name if obj.expert else ""


class ProjectDetailSerializer(ProjectListSerializer):
    client_name = serializers.SerializerMethodField()
    expert_role = serializers.SerializerMethodField()
    business_developer_name = serializers.SerializerMethodField()
    tasks = TaskSerializer(many=True, read_only=True)
    activity = ActivitySerializer(many=True, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta(ProjectListSerializer.Meta):
        fields = ProjectListSerializer.Meta.fields + [
            "client_name", "description", "timeline", "budget_range",
            "expert_role", "tasks", "activity", "attachments",
            "business_developer", "business_developer_name",
        ]
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


class AssignSerializer(serializers.Serializer):
    expert = serializers.IntegerField()
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
