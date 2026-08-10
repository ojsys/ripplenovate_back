import random
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.uploads import project_document_path, validate_project_document


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
    # Which discipline delivers this brief, and the specific offering inside it.
    # `product_line` is what routes the brief to the right delivery leads.
    product_line = models.ForeignKey(
        "catalog.ProductLine", on_delete=models.PROTECT,
        null=True, blank=True, related_name="projects",
    )
    service = models.ForeignKey(
        "catalog.Service", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="projects",
    )
    # The service name at the time of posting. Kept as plain text so an invoice
    # or a completed project still reads correctly if the catalogue is later
    # renamed or reorganised.
    category = models.CharField(max_length=100)
    timeline = models.CharField(max_length=50, blank=True)
    budget_range = models.CharField(max_length=50, blank=True)
    description = models.TextField()
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.SUBMITTED)
    quote_usd = models.PositiveIntegerField(default=0)
    # The expert who owns delivery. One of `experts`, always — the team is the
    # M2M below; this names the person answerable for the whole brief.
    #
    # It also carries the legacy payout path: a project with no priced tasks
    # pays its entire expert share to this person on completion, which is what
    # every project did before task payouts existed.
    expert = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="primary_expert_projects",
        verbose_name="Primary expert",
    )
    # Everyone delivering this brief. A lead builds a team here and then splits
    # the expert share across tasks assigned to its members.
    experts = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="assigned_projects",
        limit_choices_to={"role": "expert"},
        help_text="The experts delivering this project. Tasks can only be "
                  "assigned to someone on this list.",
    )
    # The delivery lead who quoted the brief — earns the lead share on completion.
    lead = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="led_projects",
    )
    # Per-project payout overrides. Null means "use the site default", so the
    # usual case stays in one place and only exceptional projects carry a number.
    SHARE_VALIDATORS = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))]
    expert_share_percent = models.DecimalField(
        "Expert share (%)", max_digits=5, decimal_places=2,
        null=True, blank=True, validators=SHARE_VALIDATORS,
        help_text="Leave blank to use the site default. Set a value to pay this "
                  "project differently — useful for a large or unusual build.",
    )
    delivery_lead_share_percent = models.DecimalField(
        "Delivery lead share (%)", max_digits=5, decimal_places=2,
        null=True, blank=True, validators=SHARE_VALIDATORS,
        help_text="Leave blank to use the site default. The platform keeps "
                  "whatever the expert and lead shares don't claim.",
    )
    # The business developer who sourced this project. Set from the client's
    # referral by default; a lead or admin can correct it until the client pays.
    business_developer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="sourced_projects",
    )
    business_dev_share_percent = models.DecimalField(
        "Business developer commission (%)", max_digits=5, decimal_places=2,
        null=True, blank=True, validators=SHARE_VALIDATORS,
        help_text="Leave blank to use the site default. Only charged when a "
                  "business developer is attributed to this project.",
    )
    # A real date, so "was it delivered on time?" is answerable. Null means the
    # date hasn't been agreed yet — which is different from a date that's passed,
    # and the two must never collapse into one another.
    target_date = models.DateField(
        "Target date", null=True, blank=True,
        help_text="When delivery is promised. Leave blank until it's agreed.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Stamped when the client (or a lead) approves delivery. Without it there is
    # no honest way to measure how long a project actually took — `created_at`
    # alone can't tell you when it finished.
    completed_at = models.DateTimeField(null=True, blank=True)

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
        # Only the overrides that are actually set constrain each other; a blank
        # field follows the site default, which is validated in its own clean().
        parts = [self.expert_share_percent, self.delivery_lead_share_percent,
                 self.business_dev_share_percent]
        total = sum((p for p in parts if p is not None), Decimal("0"))
        if total > Decimal("100"):
            raise ValidationError(
                "The expert, delivery lead and business developer shares can't add "
                f"up to more than 100% of the quote (currently {total}%)."
            )
        # Shrinking the expert share below what's already promised to tasks
        # would leave the project owing more than it holds. The API refuses
        # over-allocation on the way in; this is the same rule reached from the
        # other direction, which is the one the Django admin can take.
        if self.pk:
            allocated = self.allocated_usd
            if allocated > self.expert_pool_usd:
                raise ValidationError({
                    "expert_share_percent": (
                        f"This project's tasks already allocate ${allocated:,.2f}, "
                        f"which is more than a {self.expert_share_percent}% share "
                        f"of a ${self.quote_usd:,} quote (${self.expert_pool_usd:,.2f}). "
                        "Re-price the tasks first."
                    )
                })

    def payout_split(self):
        """How this project's quote divides — expert, lead, business dev, platform.

        A per-project override wins; anything left blank falls back to the site
        default. The platform's cut is the remainder, never a stored percentage,
        so the shares always total the quote exactly.

        **The business developer commission is only charged when one is
        attributed.** On a direct project their share is 0% and the platform
        keeps it — which needs no special case, because the platform is the
        remainder.

        Once a project is approved the figures come from the credited Earning
        rows instead — those are snapshots of what was actually paid, so
        changing an override later can't retroactively rewrite history (or leave
        the platform's cut disagreeing with the ledger).
        """
        from accounts.models import SiteSettings  # local: keeps app imports one-way

        cfg = SiteSettings.payout_config()
        expert_pct = (self.expert_share_percent
                      if self.expert_share_percent is not None
                      else Decimal(cfg["expert_share_percent"]))
        lead_pct = (self.delivery_lead_share_percent
                    if self.delivery_lead_share_percent is not None
                    else Decimal(cfg["delivery_lead_share_percent"]))
        if self.business_developer_id:
            bizdev_pct = (self.business_dev_share_percent
                          if self.business_dev_share_percent is not None
                          else Decimal(cfg["business_dev_share_percent"]))
        else:
            bizdev_pct = Decimal("0")
        quote = _money(self.quote_usd)

        credited = {}
        if self.stage == self.Stage.COMPLETED:
            for earning in self.earnings.all():
                amount, pct = credited.get(earning.kind, (Decimal("0"), Decimal("0")))
                # Both totals accumulate. The expert share can now arrive as one
                # row per approved task, and each row's `share_percent` is that
                # task's slice of the quote — so they sum to the share actually
                # paid, exactly as a single legacy row's percent did on its own.
                # Taking the percent from whichever row came first would have
                # reported one task's slice as the whole expert share.
                credited[earning.kind] = (amount + earning.amount_usd,
                                          pct + earning.share_percent)

        def part(kind, pct):
            """What was credited if it has been, else what's projected."""
            if kind in credited:
                amount, credited_pct = credited[kind]
                # Report the snapshot, so a later override can't restate history.
                return _money(amount), credited_pct
            return _money(quote * pct / Decimal(100)), pct

        expert_usd, expert_pct = part("expert", expert_pct)
        lead_usd, lead_pct = part("delivery_lead", lead_pct)
        bizdev_usd, bizdev_pct = part("business_dev", bizdev_pct)
        platform_usd = _money(quote - expert_usd - lead_usd - bizdev_usd)
        return {
            "quote_usd": quote,
            "expert_percent": expert_pct,
            "expert_usd": expert_usd,
            "delivery_lead_percent": lead_pct,
            "delivery_lead_usd": lead_usd,
            "business_dev_percent": bizdev_pct,
            "business_dev_usd": bizdev_usd,
            "has_business_dev": bool(self.business_developer_id),
            # Both derived from the others, so the split always closes exactly
            # — including any rounding.
            "platform_percent": _money(Decimal("100") - expert_pct - lead_pct - bizdev_pct),
            "platform_usd": platform_usd,
            "uses_override": (self.expert_share_percent is not None
                              or self.delivery_lead_share_percent is not None
                              or self.business_dev_share_percent is not None),
            "is_settled": bool(credited),
        }

    @property
    def expert_pool_usd(self):
        """The whole expert share of the quote, before it's split across tasks.

        Computed from the percentages alone, deliberately. `payout_split()`
        switches to reporting credited amounts once a project completes — right
        for reporting, wrong for allocation, which has to keep answering the
        same question after the money has moved.
        """
        from accounts.models import SiteSettings  # local: keeps app imports one-way

        pct = (self.expert_share_percent
               if self.expert_share_percent is not None
               else Decimal(SiteSettings.payout_config()["expert_share_percent"]))
        return _money(_money(self.quote_usd) * pct / Decimal(100))

    @property
    def allocated_usd(self):
        """What the priced tasks on this project add up to."""
        # Summed in Python rather than aggregated, so a prefetched task list
        # (the detail view, the board) doesn't trigger another query per project.
        return _money(sum((t.amount_usd for t in self.tasks.all()), Decimal("0")))

    @property
    def unallocated_usd(self):
        """Pool left to hand out. Negative would mean over-allocation, which the
        task write path refuses — this is the number the UI shows a lead."""
        return _money(self.expert_pool_usd - self.allocated_usd)

    @property
    def uses_task_payouts(self):
        """Whether experts on this project are paid per task.

        Derived rather than flagged, so it can't drift out of step with the
        tasks themselves. A project with nothing priced takes the legacy path:
        the whole expert share to `expert` when the client approves delivery.
        """
        return any(t.amount_usd > 0 for t in self.tasks.all())

    @property
    def cycle_days(self):
        """Calendar days from brief to approval, or None if still running."""
        if not self.completed_at:
            return None
        return max((self.completed_at - self.created_at).days, 0)

    @property
    def is_on_time(self):
        """Was it delivered by the promised date?

        None when the question doesn't apply — no target agreed, or not
        delivered yet. Callers must treat None as "excluded from the sample",
        never as a miss: a project with no promised date can't break a promise.
        """
        if not self.target_date or not self.completed_at:
            return None
        return self.completed_at.date() <= self.target_date

    @property
    def days_late(self):
        """How far past the target it landed. Negative means early."""
        if not self.target_date or not self.completed_at:
            return None
        return (self.completed_at.date() - self.target_date).days

    @property
    def is_overdue(self):
        """Past its target and still not delivered.

        False (not None) when there's no target, so the board can treat this as
        a plain flag — an unagreed date is not an overdue one.
        """
        if not self.target_date or self.stage == self.Stage.COMPLETED:
            return False
        return timezone.localdate() > self.target_date

    @property
    def days_overdue(self):
        """How many days past the target a live project is. None if it isn't.

        Derived server-side alongside `is_overdue` rather than recomputed in the
        browser, so the flag and the number are always answering from the same
        clock.
        """
        if not self.is_overdue:
            return None
        return (timezone.localdate() - self.target_date).days

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
    """A unit of work on a project, assigned to one expert and worth an amount.

    A task carries money. The expert does the work and submits it; the delivery
    lead checks it and approves it; approving is what credits the expert's
    earning. That second pair of eyes is the point — it's the same rule payouts
    already follow, where nobody settles their own withdrawal.

    `amount_usd` is drawn from the project's expert share. The sum of a
    project's tasks can never exceed `Project.expert_pool_usd`, so the platform
    can't be made to pay out more than it took in.
    """

    class Status(models.TextChoices):
        TODO = "todo", "To do"
        SUBMITTED = "submitted", "Submitted for review"
        CHANGES = "changes", "Changes requested"
        # Terminal, and the reason there is no way back: approval releases the
        # money, which may be withdrawn before anyone changes their mind.
        APPROVED = "approved", "Approved"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=255)
    # What "done" means for this task. Vague tasks are what approval disputes
    # are made of, and there's money on the other side of this one.
    description = models.TextField(blank=True)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks",
    )
    amount_usd = models.DecimalField(
        "Amount (USD)", max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What the assignee earns when this task is approved. Comes out "
                  "of the project's expert share.",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.TODO)
    order = models.PositiveIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="approved_tasks",
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_usd__gte=Decimal("0")),
                name="task_amount_not_negative",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def done(self):
        """Kept so everything reading tasks — the board, progress, the mobile
        list — keeps working while the lifecycle grows underneath it."""
        return self.status == self.Status.APPROVED

    @property
    def is_paid_work(self):
        """Whether approving this task will move money."""
        return self.amount_usd > 0


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


class Attachment(models.Model):
    """A link to something that lives outside the platform.

    Design, research and content work isn't handed over as a checked-off task —
    it's a Figma file, a deck, a document. Without somewhere to put those, a
    non-software project has no way to actually deliver anything, and the
    conversation drifts to email where nobody else can see it.

    An attachment is **either a link or an uploaded file** — the same row type
    either way, so everything that reads attachments (the deliverables panel, the
    activity feed, the brief) handles both without knowing the difference.

    Links came first because that's how designers already share work. Uploads
    matter for the other half: a client attaching a scoping document or a
    signed brief has a file, not a Figma URL.
    """

    class Kind(models.TextChoices):
        FIGMA = "figma", "Figma"
        DRIVE = "drive", "Google Drive"
        DROPBOX = "dropbox", "Dropbox"
        GITHUB = "github", "GitHub"
        NOTION = "notion", "Notion"
        LOOM = "loom", "Loom"
        LINK = "link", "Link"
        FILE = "file", "Uploaded file"

    class Purpose(models.TextChoices):
        # What the client supplied: brand assets, existing research, examples.
        REFERENCE = "reference", "Reference"
        # What the team produced: the actual work being handed over.
        DELIVERABLE = "deliverable", "Deliverable"

    # Host fragments that identify a link's source, longest-first so that a more
    # specific match wins.
    HOST_KINDS = [
        ("figma.com", Kind.FIGMA),
        ("drive.google.com", Kind.DRIVE),
        ("docs.google.com", Kind.DRIVE),
        ("dropbox.com", Kind.DROPBOX),
        ("github.com", Kind.GITHUB),
        ("notion.so", Kind.NOTION),
        ("notion.site", Kind.NOTION),
        ("loom.com", Kind.LOOM),
    ]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="attachments"
    )
    # Set when the link was shared as part of a progress update, so the feed can
    # show it inline and the deliverables panel can say where it came from.
    activity = models.ForeignKey(
        Activity, on_delete=models.CASCADE,
        null=True, blank=True, related_name="attachments",
    )
    # Exactly one of these carries the content. `url` for a link, `file` for an
    # upload; `clean()` enforces that one and only one is set.
    url = models.URLField(max_length=1000, blank=True)
    file = models.FileField(
        upload_to=project_document_path, blank=True, null=True,
        validators=[validate_project_document],
        help_text="Streamed through an authenticated view — never a public URL.",
    )
    original_filename = models.CharField(max_length=255, blank=True)
    size_bytes = models.PositiveIntegerField(null=True, blank=True)
    label = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.LINK)
    purpose = models.CharField(
        max_length=12, choices=Purpose.choices, default=Purpose.DELIVERABLE
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.label or self.url} ({self.get_kind_display()})"

    @property
    def is_file(self):
        return bool(self.file)

    def clean(self):
        if bool(self.url) == bool(self.file):
            raise ValidationError(
                "An attachment is either a link or an uploaded file — set one, not both."
            )

    @classmethod
    def detect_kind(cls, url):
        """Work out what a link points at, so the UI can label and icon it."""
        host = urlparse(url or "").netloc.lower()
        for fragment, kind in cls.HOST_KINDS:
            if host == fragment or host.endswith("." + fragment):
                return kind
        return cls.Kind.LINK

    def save(self, *args, **kwargs):
        if self.file:
            self.kind = self.Kind.FILE
            if not self.label:
                self.label = self.original_filename or "Document"
        else:
            if not self.kind or self.kind == self.Kind.LINK:
                self.kind = self.detect_kind(self.url)
            if not self.label:
                # A bare host reads better in a list than a 200-character URL.
                self.label = urlparse(self.url).netloc or self.url[:200]
        super().save(*args, **kwargs)
