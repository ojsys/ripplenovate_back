import random
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    # The delivery lead who quoted the brief — earns the lead share on completion.
    lead = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="led_projects",
    )
    # Per-project payout overrides. Null means "use the site default", so the
    # usual case stays in one place and only exceptional projects carry a number.
    SHARE_VALIDATORS = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))]
    developer_share_percent = models.DecimalField(
        "Developer share (%)", max_digits=5, decimal_places=2,
        null=True, blank=True, validators=SHARE_VALIDATORS,
        help_text="Leave blank to use the site default. Set a value to pay this "
                  "project differently — useful for a large or unusual build.",
    )
    delivery_lead_share_percent = models.DecimalField(
        "Delivery lead share (%)", max_digits=5, decimal_places=2,
        null=True, blank=True, validators=SHARE_VALIDATORS,
        help_text="Leave blank to use the site default. The platform keeps "
                  "whatever the developer and lead shares don't claim.",
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

    def clean(self):
        dev = self.developer_share_percent
        lead = self.delivery_lead_share_percent
        if dev is not None and lead is not None and dev + lead > Decimal("100"):
            raise ValidationError(
                "The developer and delivery lead shares can't add up to more than "
                f"100% of the quote (currently {dev + lead}%)."
            )

    def payout_split(self):
        """How this project's quote divides — developer, delivery lead, platform.

        A per-project override wins; anything left blank falls back to the site
        default. The platform's cut is the remainder, never a stored percentage,
        so the three shares always total the quote exactly.

        Once a project is approved the developer/lead figures come from the
        credited Earning rows instead — those are snapshots of what was actually
        paid, so changing an override later can't retroactively rewrite history
        (or leave the platform's cut disagreeing with the ledger).
        """
        from accounts.models import SiteSettings  # local: keeps app imports one-way

        cfg = SiteSettings.payout_config()
        dev_pct = (self.developer_share_percent
                   if self.developer_share_percent is not None
                   else Decimal(cfg["developer_share_percent"]))
        lead_pct = (self.delivery_lead_share_percent
                    if self.delivery_lead_share_percent is not None
                    else Decimal(cfg["delivery_lead_share_percent"]))
        quote = _money(self.quote_usd)

        credited = {}
        if self.stage == self.Stage.COMPLETED:
            for earning in self.earnings.all():
                amount, pct = credited.get(earning.kind, (Decimal("0"), earning.share_percent))
                credited[earning.kind] = (amount + earning.amount_usd, pct)

        def part(kind, pct):
            """What was credited if it has been, else what's projected."""
            if kind in credited:
                amount, credited_pct = credited[kind]
                # Report the snapshot, so a later override can't restate history.
                return _money(amount), credited_pct
            return _money(quote * pct / Decimal(100)), pct

        dev_usd, dev_pct = part("developer", dev_pct)
        lead_usd, lead_pct = part("delivery_lead", lead_pct)
        platform_usd = _money(quote - dev_usd - lead_usd)
        return {
            "quote_usd": quote,
            "developer_percent": dev_pct,
            "developer_usd": dev_usd,
            "delivery_lead_percent": lead_pct,
            "delivery_lead_usd": lead_usd,
            # Both derived from the other two, so the split always closes exactly
            # — including any rounding.
            "platform_percent": _money(Decimal("100") - dev_pct - lead_pct),
            "platform_usd": platform_usd,
            "uses_override": (self.developer_share_percent is not None
                              or self.delivery_lead_share_percent is not None),
            "is_settled": bool(credited),
        }

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
