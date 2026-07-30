from rest_framework import serializers

from .models import Activity, Project, Task


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


class ActivitySerializer(serializers.ModelSerializer):
    initials = serializers.SerializerMethodField()

    class Meta:
        model = Activity
        fields = ["id", "author_name", "role_label", "kind", "text", "created_at", "initials"]

    def get_initials(self, obj):
        return _initials(obj.author_name)


class ProjectListSerializer(serializers.ModelSerializer):
    developer_name = serializers.SerializerMethodField()
    progress_pct = serializers.IntegerField(read_only=True)
    is_paid = serializers.BooleanField(read_only=True)
    stage_index = serializers.IntegerField(read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "code", "title", "company", "category", "stage", "stage_index",
            "quote_usd", "developer", "developer_name", "progress_pct", "is_paid",
            "target_date", "created_at",
        ]
        # Output only. Lifecycle fields are moved by the viewset's actions (which
        # log activity and notify), never written straight from a request body.
        read_only_fields = [
            "code", "title", "company", "category", "stage", "quote_usd",
            "developer", "target_date", "created_at",
        ]

    def get_developer_name(self, obj):
        return obj.developer.full_name if obj.developer else ""


class ProjectDetailSerializer(ProjectListSerializer):
    client_name = serializers.SerializerMethodField()
    developer_role = serializers.SerializerMethodField()
    tasks = TaskSerializer(many=True, read_only=True)
    activity = ActivitySerializer(many=True, read_only=True)

    class Meta(ProjectListSerializer.Meta):
        fields = ProjectListSerializer.Meta.fields + [
            "client_name", "description", "timeline", "budget_range",
            "developer_role", "tasks", "activity",
        ]
        read_only_fields = ProjectListSerializer.Meta.read_only_fields + [
            "description", "timeline", "budget_range",
        ]

    def get_client_name(self, obj):
        return obj.client.full_name or obj.client.email

    def get_developer_role(self, obj):
        return obj.developer.specialty if obj.developer else ""


class ProjectCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["title", "category", "timeline", "budget_range", "description"]

    def validate_title(self, v):
        if not v.strip():
            raise serializers.ValidationError("Add a project title.")
        return v.strip()

    def validate_description(self, v):
        if not v.strip():
            raise serializers.ValidationError("Describe what we're building.")
        return v.strip()


class ProjectEditSerializer(serializers.ModelSerializer):
    """A delivery lead correcting a brief, or re-pricing a quote.

    Only the fields a lead legitimately maintains. `stage`, `developer` and
    `client` are absent on purpose — those move through the lifecycle actions,
    which log activity and notify the people involved.
    """

    class Meta:
        model = Project
        fields = ["title", "category", "timeline", "budget_range", "description",
                  "target_date", "quote_usd"]
        extra_kwargs = {field: {"required": False} for field in fields}

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
    developer = serializers.IntegerField()
    tasks = serializers.ListField(
        child=serializers.CharField(allow_blank=True), required=False, default=list
    )


class ActivityCreateSerializer(serializers.Serializer):
    text = serializers.CharField()
    kind = serializers.ChoiceField(
        choices=[k for k in Activity.POSTABLE_KINDS],
        default=Activity.Kind.UPDATE,
    )
