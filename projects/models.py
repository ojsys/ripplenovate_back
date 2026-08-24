import random
from datetime import timedelta
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
        # Terminal, and deliberately outside STAGE_ORDER: cancelling isn't a
        # step further along the lifecycle, it's leaving it. Anything that reads
        # progress as a position in the order would otherwise report a killed
        # project as 0% and in flight.
        CANCELLED = "Cancelled", "Cancelled"

    # Ordered lifecycle used for progress calculation.
    STAGE_ORDER = [
        Stage.SUBMITTED, Stage.QUOTED, Stage.PAID,
        Stage.IN_PROGRESS, Stage.REVIEW, Stage.COMPLETED,
    ]
    PAID_STAGES = {Stage.PAID, Stage.IN_PROGRESS, Stage.REVIEW, Stage.COMPLETED}
    # Work that has stopped, either way. Reporting asks "is this still running?"
    # far more often than it asks "did it finish?", and before cancellation
    # existed the two questions had the same answer — so every `exclude
    # (COMPLETED)` in the codebase silently meant "in flight". This is that
    # question, named.
    CLOSED_STAGES = {Stage.COMPLETED, Stage.CANCELLED}

    code = models.CharField(max_length=20, unique=True, default=generate_code, editable=False)
    title = models.CharField(max_length=200)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="client_projects"
    )
    company = models.CharField(max_length=150, blank=True)
    # Set when this project is one month of a retainer. Null on a standalone
    # brief, which is every project written before engagements existed — and
    # the reason nothing else in the payout, reporting or lifecycle code needed
    # to change: a cycle is an ordinary project that happens to know why it
    # exists.
    engagement = models.ForeignKey(
        "Engagement", on_delete=models.PROTECT,
        null=True, blank=True, related_name="cycles",
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    # The buying company. `client` stays as the individual who posted the brief
    # — replacing it with the org would lose who is actually accountable on the
    # buyer's side, which is the person the team talks to.
    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT,
        null=True, blank=True, related_name="projects",
    )
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
    # How many times the client has sent the work back. Denormalised from
    # `revision_requests` so the delivery board can show it without a join per
    # row, and so reporting can ask "which lines bounce most?" cheaply.
    revision_rounds = models.PositiveIntegerField(default=0)
    # When the team first nudged the client about a waiting review. This is what
    # starts the clock on closing a project over a silent client's head — so a
    # lead has to actually ask before they can decide nobody answered.
    review_reminded_at = models.DateTimeField(null=True, blank=True)
    # Who signed the work off. Null on projects completed before this was
    # tracked. When it isn't the client, `countersigned_by` says whether a
    # second lead authorised it or the silence window ran out.
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="projects_completed",
    )
    countersigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="projects_countersigned",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="projects_cancelled",
    )
    # Always required when cancelling. A project that stopped without a recorded
    # reason is one nobody can learn anything from afterwards.
    cancellation_reason = models.TextField(blank=True)

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
        # Shares are taken from the whole contract, not just the original quote,
        # so paid extra scope pays everybody in the same proportions. Equal to
        # the quote whenever there are no change orders.
        contract = self.contract_usd

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
            return _money(contract * pct / Decimal(100)), pct

        expert_usd, expert_pct = part("expert", expert_pct)
        lead_usd, lead_pct = part("delivery_lead", lead_pct)
        bizdev_usd, bizdev_pct = part("business_dev", bizdev_pct)
        platform_usd = _money(contract - expert_usd - lead_usd - bizdev_usd)
        return {
            "quote_usd": quote,
            # Kept separate from the quote so the client's original invoice and
            # the project's current worth are never confused for one another.
            "change_orders_usd": _money(contract - quote),
            "contract_usd": contract,
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
    def change_orders_usd(self):
        """Extra scope the client has paid for on top of the quote."""
        return _money(sum(
            (c.amount_usd for c in self.change_orders.all()
             if c.status == ChangeOrder.Status.PAID),
            Decimal("0"),
        ))

    @property
    def contract_usd(self):
        """What this project is worth in total — the quote plus paid extras.

        The base every share is taken from. `quote_usd` stays exactly what it
        always was (the price the client agreed and paid at kickoff, immutable
        once settled); this is that plus anything they've since paid for on
        top. A project with no change orders returns the quote unchanged, which
        is why nothing else in the payout path needed a special case.
        """
        return _money(_money(self.quote_usd) + self.change_orders_usd)

    @property
    def collected_usd(self):
        """What the client has actually paid us, in USD.

        Read from successful payments rather than from `quote_usd`, because a
        refund is bounded by money that genuinely arrived — not by what we said
        it would cost.
        """
        # The literal rather than `Payment.Status.SUCCESS`: payments imports
        # projects, so importing back would close the loop. Pinned by
        # `test_payment_success_literal_matches_the_enum`, which fails loudly
        # if that value is ever renamed.
        total = sum(
            (p.usd_total for p in self.payments.all()
             if p.status == "success"),
            Decimal("0"),
        )
        return _money(total)

    @property
    def released_usd(self):
        """Everything credited out of this project to a person.

        Task payments already banked, plus the lead and business developer
        shares once the project completed. This is the floor under a refund:
        the platform can hand back what it still holds without anyone losing
        money, and beyond that the reserve has to cover it.
        """
        return _money(sum((e.amount_usd for e in self.earnings.all()),
                          Decimal("0")))

    @property
    def refunded_usd(self):
        """What has already gone back to the client on this project."""
        return _money(sum(
            (r.amount_usd for r in self.refunds.all() if r.is_settled),
            Decimal("0"),
        ))

    @property
    def refundable_usd(self):
        """The most that can still be refunded — everything not yet returned.

        Deliberately *not* reduced by what's been released. Money already paid
        out limits what can be refunded painlessly, not what can be refunded at
        all; the difference comes out of the reserve, and beyond that the
        platform absorbs it. Capping here would mean a project that failed after
        the experts were paid could never be made right, which is precisely the
        case a refund policy exists for.
        """
        return _money(max(self.collected_usd - self.refunded_usd, Decimal("0")))

    @property
    def free_refund_usd(self):
        """How much can be refunded without touching the reserve."""
        held = self.collected_usd - self.released_usd - self.refunded_usd
        return _money(max(held, Decimal("0")))

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
        # Off the contract, so paying for extra scope actually gives the lead
        # something to hand out for it. Off the quote alone, a change order
        # would raise the platform's take and leave the pool untouched — which
        # is the opposite of what the client just bought.
        return _money(self.contract_usd * pct / Decimal(100))

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
    def open_revision(self):
        """The revision the client is still waiting on, or None.

        Open means requested and not yet resubmitted. Two things read this: the
        client's project page, so the request stays visible until it's answered,
        and completion, which a lead may not force while one stands.
        """
        return self.revision_requests.filter(resolved_at__isnull=True).first()

    def client_silence_block(self):
        """Why a lead may not close this over the client's head — or None.

        Returns the reason as a sentence the API can hand straight to the
        person, because every one of these is something they can act on rather
        than a policy to look up.

        The rule has three parts, and each exists for its own reason:

        * **They must have asked.** Without a reminder there is no evidence the
          client was ever given the chance, and "they went quiet" is a claim
          about a conversation that never happened.
        * **The client must actually be silent.** Anything they've said since
          the reminder resets the clock. A client who is mid-discussion is not
          absent, and closing on them would be the worst version of this.
        * **No revision may be outstanding.** They told you it wasn't right;
          completing anyway takes payment for work they explicitly rejected.
        """
        # Imported here rather than at module scope: accounts imports projects
        # for the payout maths, so a top-level import closes the loop.
        from accounts.models import SiteSettings

        if self.open_revision:
            return ("The client has asked for changes. Make them and resubmit — "
                    "you can't complete a project over an open change request.")
        if not self.review_reminded_at:
            return ("Remind the client first. They can't be counted as silent "
                    "until they've been asked.")

        # Anything from the client since the nudge means they're present.
        spoke_since = self.activity.filter(
            author_id=self.client_id, created_at__gt=self.review_reminded_at
        ).exists()
        if spoke_since:
            return ("The client has been in touch since your reminder, so they "
                    "aren't out of contact. Ask them to approve, or sort out "
                    "what's outstanding.")

        days = SiteSettings.load().client_silence_days
        ready_at = self.review_reminded_at + timedelta(days=days)
        now = timezone.now()
        if now < ready_at:
            remaining = max((ready_at - now).days, 0) + 1
            return (f"It's only been {(now - self.review_reminded_at).days} of "
                    f"{days} days since you reminded them. You can complete this "
                    f"in about {remaining} day{'s' if remaining != 1 else ''}, or "
                    "an administrator can countersign it now.")
        return None

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
        # A cancelled project isn't late, it's over. Leaving it overdue would
        # put a red chip on the board forever for work nobody is doing.
        if not self.target_date or self.stage in self.CLOSED_STAGES:
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
        # Cancelled work stops where it stopped. Reporting 100% would claim it
        # was delivered; reporting 0% (which `stage_index` would give, since
        # Cancelled isn't in STAGE_ORDER) would erase the work that was done.
        if self.stage == self.Stage.CANCELLED:
            if total:
                return round(done / total * 100)
            return round(self.STAGE_ORDER.index(self.Stage.PAID) / 5 * 100) if self.is_paid else 0
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


class ProjectFeedback(models.Model):
    """What the client thought, kept private.

    Deliberately not a public rating. Public stars would turn the roster into a
    leaderboard, put experts in competition with each other, and hand a
    departing lead a portable reputation — all things this platform is built to
    avoid. But going that far and capturing the client's opinion *nowhere* left
    no way to tell a good lead from a lucky one: on-time rate and cycle time
    measure delivery, not whether anybody was happy with it.

    Read by the project's own lead and by admins. Never by experts, never by
    other leads, never shown to another client.

    A row can also be written when a project is *cancelled*, which is where the
    reason matters most and where nobody is otherwise inclined to ask.
    """

    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="feedback"
    )
    # 1–5. Left deliberately coarse: a client answering in five seconds gives a
    # more honest number than one asked to weigh seven dimensions.
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True)
    # The question that actually predicts revenue, asked separately because a
    # 4-star project they'd never repeat and a 4-star project they'd rebook are
    # different businesses.
    would_work_again = models.BooleanField(null=True, blank=True)
    # Whether this client agreed we may quote them publicly.
    #
    # Default False and asked explicitly, because the form that collects this
    # feedback promises it goes to the delivery lead and nobody else. Publishing
    # on the strength of that would break the promise the words were written
    # under — consent given for one audience is not consent for another.
    may_publish = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "project feedback"

    def __str__(self):
        return f"{self.project.code} · {self.rating}/5"


class ChangeOrder(models.Model):
    """Extra scope on a project the client has already paid for.

    `quote_usd` locks at payment, and it must: the invoice the client settled
    and the earnings snapshotted on approval both derive from it. But that left
    no answer at all to "we need more than we scoped" except free work or a
    second brief — which is why leads quietly absorbed scope and why the
    revision loop had nowhere to send genuine growth.

    A change order is separately priced, separately paid, and adds to the
    project's **contract value** rather than editing the quote. Every share
    then applies to it exactly as it applies to the original: the expert pool
    grows, the lead's cut grows, the platform's remainder grows. No new payout
    arithmetic, and the quote stays the immutable thing it needs to be.

    Only on live paid work. Once a project completes, earnings are snapshotted
    and `payout_split()` reports what was credited rather than what's owed —
    adding contract value after that would make the two disagree.
    """

    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting", "Awaiting payment"
        PAID = "paid", "Paid"
        WITHDRAWN = "withdrawn", "Withdrawn"

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="change_orders"
    )
    # What the extra work is. Shown to the client on the payment screen, so it
    # has to stand on its own without the conversation that produced it.
    description = models.TextField()
    amount_usd = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.AWAITING_PAYMENT
    )
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="change_orders_raised",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.project.code} · +${self.amount_usd} · {self.status}"

    @property
    def is_payable(self):
        return self.status == self.Status.AWAITING_PAYMENT


class RevisionRequest(models.Model):
    """The client sending delivered work back, with a reason.

    One row per round. A counter on the project would have been cheaper, but it
    can't answer the two questions that matter after the fact — *what* was wrong,
    and *how long* the team took to turn it around — and those are exactly what
    a lead needs when a project has bounced three times.

    A row is open until the team resubmits for review, and an open row is what
    stops a lead completing the project over the client's head.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="revision_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="revision_requests",
    )
    # Mandatory at the API. "Send it back" with no reason is how a revision loop
    # turns into an argument.
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    # Stamped when the team submits for review again. Null means the work is
    # still out with them.
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        state = "open" if self.resolved_at is None else "resolved"
        return f"{self.project.code} · revision ({state})"


class Activity(models.Model):
    """An entry in a project's activity feed."""

    class Kind(models.TextChoices):
        SYSTEM = "system", "System"          # auto-generated lifecycle events
        UPDATE = "update", "Update"          # general note
        PROGRESS = "progress", "Progress"    # work moved forward
        MILESTONE = "milestone", "Milestone" # something shipped / delivered
        BLOCKER = "blocker", "Blocker"       # something is blocked / at risk
        QUESTION = "question", "Question"    # needs a decision / input
        REVISION = "revision", "Changes requested"  # client sent the work back

    # Kinds a person may choose when posting an update (excludes SYSTEM).
    # REVISION is absent on purpose: it isn't something you *post*, it's what
    # requesting changes writes, so the composer must not offer it.
    POSTABLE_KINDS = [Kind.PROGRESS, Kind.MILESTONE, Kind.BLOCKER, Kind.QUESTION, Kind.UPDATE]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="activity")
    # What this is a reply to. One level only, enforced on the write path: a
    # reply to a reply re-parents to the same top-level entry. Arbitrary nesting
    # would make the feed a tree to render and a recursive query to fetch, and
    # nobody has ever needed the fourth level of a project comment thread.
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True,
        related_name="replies",
    )
    # The deliverable this is about. Anchoring matters more than threading here:
    # most bounced work is a misunderstanding about one specific file, and
    # "the second slide is wrong" in a flat feed of twelve updates is a
    # different message from the same words attached to the deck.
    attachment = models.ForeignKey(
        "Attachment", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="comments",
    )
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

    @property
    def thread_emails(self):
        """Everyone already talking in this thread.

        The parent's author plus every replier. Used instead of the whole
        project team so a two-person exchange about one file doesn't email six
        people nine times — which is how a feed people read becomes a filter
        rule they don't.
        """
        root = self.parent or self
        people = {root.author} | {r.author for r in root.replies.all()}
        return {p.email for p in people if p is not None}

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

    @property
    def display_name(self):
        """What to call this in a sentence.

        The label if someone gave it one, else the uploaded filename, else the
        URL. A comment that says "About: https://figma.com/file/aB3x9…" is
        still more use than one that says "About: ".
        """
        return self.label or self.original_filename or self.url or "the attachment"

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


class Engagement(models.Model):
    """Ongoing work billed monthly — a retainer.

    Holds only what doesn't change month to month: who it's for, who runs it,
    what it costs, and when it bills. Everything that *does* change — the
    tasks, the deliverables, the conversation, the money — lives on the
    monthly `Project` cycles, because that is where all of it already worked.

    Ending an engagement stops future cycles and touches nothing already
    delivered. Pausing skips generation without ending it, which is what a
    client going quiet over August actually wants.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        ENDED = "ended", "Ended"

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT,
        related_name="engagements",
    )
    # The individual at the client who set it up — the person the team talks
    # to, exactly as `Project.client` is on a one-off brief.
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="client_engagements",
    )
    lead = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="led_engagements",
    )
    product_line = models.ForeignKey(
        "catalog.ProductLine", on_delete=models.PROTECT, related_name="engagements",
    )
    service = models.ForeignKey(
        "catalog.Service", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="engagements",
    )
    title = models.CharField(max_length=200)
    description = models.TextField()
    monthly_amount_usd = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("1"))],
    )
    # Capped at 28 so every month has one. A retainer that silently skips
    # February is a support ticket nobody should have to raise.
    billing_day = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text="Day of the month each period starts. 1–28.",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    started_on = models.DateField()
    ends_on = models.DateField(
        null=True, blank=True,
        help_text="Leave blank to run until somebody ends it.",
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.title} · ${self.monthly_amount_usd}/mo"

    @property
    def is_live(self):
        return self.status == self.Status.ACTIVE

    @property
    def delivered_cycles(self):
        return self.cycles.filter(stage=Project.Stage.COMPLETED).count()

    @property
    def billed_usd(self):
        """Everything invoiced across every cycle, paid or not."""
        return _money(sum((c.quote_usd for c in self.cycles.all()), 0))


class CycleRun(models.Model):
    """A pass of the cycle generator, logged whether or not it did anything.

    This is the only job in the platform that creates billable records without
    a person present, so "did it fire, and what did it do?" has to be
    answerable from the admin rather than from a server log nobody can reach.
    Dry runs are recorded too — an empty log is a far clearer answer than an
    absent one.
    """

    ran_at = models.DateTimeField(auto_now_add=True)
    dry_run = models.BooleanField(default=False)
    created_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    detail = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-ran_at", "-id"]

    def __str__(self):
        kind = "dry run" if self.dry_run else "run"
        return f"Cycle {kind} · {self.created_count} created · {self.ran_at:%Y-%m-%d %H:%M}"
