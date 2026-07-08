from django.contrib.auth import get_user_model
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import notifications
from .models import Activity, Project, Task
from .serializers import (
    ActivityCreateSerializer,
    AssignSerializer,
    ProjectCreateSerializer,
    ProjectDetailSerializer,
    ProjectListSerializer,
    QuoteSerializer,
    TaskSerializer,
)

User = get_user_model()
Role = User.Role
Stage = Project.Stage


def log_activity(project, user, text, kind=Activity.Kind.SYSTEM):
    return Activity.objects.create(
        project=project,
        author=user,
        author_name=user.full_name or user.email,
        role_label=user.role_label,
        kind=kind,
        text=text,
    )


class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        base = Project.objects.select_related("client", "developer").prefetch_related(
            "tasks", "activity"
        )
        if user.role == Role.DELIVERY_LEAD or user.is_superuser:
            return base
        if user.role == Role.DEVELOPER:
            return base.filter(developer=user)
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
        fresh = Project.objects.select_related("client", "developer").prefetch_related(
            "tasks", "activity"
        ).get(pk=project.pk)
        return Response(ProjectDetailSerializer(fresh).data, status=status_code)

    def create(self, request, *args, **kwargs):
        if request.user.role != Role.CLIENT:
            raise PermissionDenied("Only clients can post projects.")
        serializer = ProjectCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.save(
            client=request.user,
            company=request.user.company,
            stage=Stage.SUBMITTED,
        )
        log_activity(project, request.user, "Submitted the project brief. Awaiting a quote.")
        notifications.notify_project_submitted(project)
        return self._detail(project, status.HTTP_201_CREATED)

    def _require_lead(self):
        if self.request.user.role != Role.DELIVERY_LEAD and not self.request.user.is_superuser:
            raise PermissionDenied("Only a delivery lead can do that.")

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
        project.save(update_fields=["quote_usd", "stage"])
        log_activity(project, request.user,
                     f"Sent a quote of ${project.quote_usd:,} — ready for payment.")
        notifications.notify_quote_sent(project)
        return self._detail(project)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        project = self.get_object()
        self._require_lead()
        if project.stage != Stage.PAID:
            raise ValidationError("A developer can only be assigned after payment.")
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dev = User.objects.filter(
            id=serializer.validated_data["developer"], role=Role.DEVELOPER
        ).first()
        if not dev:
            raise ValidationError("Select a valid developer.")
        titles = [t.strip() for t in serializer.validated_data.get("tasks", []) if t.strip()]
        project.developer = dev
        project.stage = Stage.IN_PROGRESS
        project.save(update_fields=["developer", "stage"])
        if titles:
            project.tasks.all().delete()
            Task.objects.bulk_create([
                Task(project=project, title=t, assignee=dev, order=i)
                for i, t in enumerate(titles)
            ])
        log_activity(project, request.user,
                     f"Assigned {dev.full_name} and kicked off development.")
        notifications.notify_developer_assigned(project)
        return self._detail(project)

    @action(detail=True, methods=["post"], url_path="submit-review")
    def submit_review(self, request, pk=None):
        project = self.get_object()
        if request.user.role != Role.DEVELOPER or project.developer_id != request.user.id:
            raise PermissionDenied("Only the assigned developer can submit for review.")
        if project.stage != Stage.IN_PROGRESS:
            raise ValidationError("This project isn't in progress.")
        project.stage = Stage.REVIEW
        project.save(update_fields=["stage"])
        log_activity(project, request.user, "Submitted the work for client review.")
        notifications.notify_submitted_for_review(project)
        return self._detail(project)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        project = self.get_object()
        if request.user.role != Role.CLIENT or project.client_id != request.user.id:
            raise PermissionDenied("Only the client can approve delivery.")
        if project.stage != Stage.REVIEW:
            raise ValidationError("This project isn't ready for approval.")
        project.stage = Stage.COMPLETED
        project.save(update_fields=["stage"])
        log_activity(project, request.user, "Approved delivery. Project complete!")
        notifications.notify_project_completed(project)
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
        notifications.notify_update_posted(project, entry)
        return self._detail(project)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def toggle_task(request, task_id):
    task = Task.objects.select_related("project", "project__developer",
                                       "project__client").filter(id=task_id).first()
    if not task:
        return Response({"detail": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
    project = task.project
    user = request.user
    allowed = (
        user.is_superuser
        or user.role == Role.DELIVERY_LEAD
        or (user.role == Role.DEVELOPER and project.developer_id == user.id)
    )
    if not allowed:
        raise PermissionDenied("You can't change tasks on this project.")
    task.done = not task.done
    task.save(update_fields=["done"])
    return Response(TaskSerializer(task).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_stats(request):
    if request.user.role != Role.DELIVERY_LEAD and not request.user.is_superuser:
        raise PermissionDenied("Delivery lead only.")
    qs = Project.objects.all()
    active = qs.exclude(stage=Stage.COMPLETED)
    return Response({
        "active_total": active.count(),
        "needs_quote": qs.filter(stage=Stage.SUBMITTED).count(),
        "needs_assign": qs.filter(stage=Stage.PAID, developer__isnull=True).count(),
        "contracted_value_usd": sum(p.quote_usd for p in active),
    })
