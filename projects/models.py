import random

from django.conf import settings
from django.db import models


def generate_code():
    """Human-friendly project code like RIL-2041, matching the prototype."""
    for _ in range(20):
        code = f"RIL-{random.randint(2050, 2999)}"
        if not Project.objects.filter(code=code).exists():
            return code
    return f"RIL-{random.randint(3000, 9999)}"


class Project(models.Model):
    """A client brief moving through the six-stage delivery lifecycle."""

    class Stage(models.TextChoices):
        SUBMITTED = "Submitted", "Submitted"
        QUOTED = "Quoted", "Quoted"
        PAID = "Paid", "Paid"
        IN_PROGRESS = "In Progress", "In Progress"
        REVIEW = "Review", "Review"
        COMPLETED = "Completed", "Completed"

    # Ordered lifecycle used for progress calculation.
    STAGE_ORDER = [
        Stage.SUBMITTED, Stage.QUOTED, Stage.PAID,
        Stage.IN_PROGRESS, Stage.REVIEW, Stage.COMPLETED,
    ]
    PAID_STAGES = {Stage.PAID, Stage.IN_PROGRESS, Stage.REVIEW, Stage.COMPLETED}

    code = models.CharField(max_length=20, unique=True, default=generate_code, editable=False)
    title = models.CharField(max_length=200)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="client_projects"
    )
    company = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=100)
    timeline = models.CharField(max_length=50, blank=True)
    budget_range = models.CharField(max_length=50, blank=True)
    description = models.TextField()
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.SUBMITTED)
    quote_usd = models.PositiveIntegerField(default=0)
    developer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="assigned_projects",
    )
    target_date = models.CharField(max_length=40, blank=True, default="TBD")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} · {self.title}"

    @property
    def stage_index(self):
        try:
            return self.STAGE_ORDER.index(self.stage)
        except ValueError:
            return 0

    @property
    def is_paid(self):
        return self.stage in self.PAID_STAGES

    @property
    def progress_pct(self):
        tasks = list(self.tasks.all())
        total = len(tasks)
        done = sum(1 for t in tasks if t.done)
        if self.stage == self.Stage.COMPLETED:
            return 100
        if total and self.stage in {self.Stage.IN_PROGRESS, self.Stage.REVIEW}:
            return round(done / total * 100)
        return round(self.stage_index / 5 * 100)


class Task(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=255)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    done = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.title


class Activity(models.Model):
    """An entry in a project's activity feed."""

    class Kind(models.TextChoices):
        SYSTEM = "system", "System"          # auto-generated lifecycle events
        UPDATE = "update", "Update"          # general note
        PROGRESS = "progress", "Progress"    # work moved forward
        MILESTONE = "milestone", "Milestone" # something shipped / delivered
        BLOCKER = "blocker", "Blocker"       # something is blocked / at risk
        QUESTION = "question", "Question"    # needs a decision / input

    # Kinds a person may choose when posting an update (excludes SYSTEM).
    POSTABLE_KINDS = [Kind.PROGRESS, Kind.MILESTONE, Kind.BLOCKER, Kind.QUESTION, Kind.UPDATE]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="activity")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    author_name = models.CharField(max_length=150)
    role_label = models.CharField(max_length=50)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.UPDATE)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name_plural = "activities"

    def __str__(self):
        return f"{self.author_name}: {self.text[:40]}"
