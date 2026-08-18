from decimal import Decimal

from django.conf import settings
from django.db import models

from projects.models import Project


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="payments")
    # Set when this payment settles extra scope rather than the original quote.
    # Null on the kickoff invoice, which is every payment that existed before
    # change orders — hence nullable rather than a separate model.
    change_order = models.ForeignKey(
        "projects.ChangeOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payments",
    )
    # Which rail carried this charge. Recorded rather than inferred, because a
    # refund has to go back the way the money came — and the platform's default
    # may well have changed by the time one is issued.
    gateway = models.CharField(max_length=20, default="paystack")
    reference = models.CharField(max_length=100, unique=True)
    access_code = models.CharField(max_length=120, blank=True)
    # Amount actually charged, in the currency's smallest unit (kobo / cents).
    amount_subunit = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=8, default="NGN")
    usd_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Rate locked in at initialization; null when the charge currency is USD.
    usd_to_ngn_rate = models.DecimalField(
        "USD → NGN rate used", max_digits=12, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} · {self.status}"


class Earning(models.Model):
    """A realized share of delivered work.

    Credited to the experts who delivered it, the delivery lead who quoted it,
    and the business developer who sourced it (when there is one).

    Rows come in two shapes:

    * **Project-level** (``task`` is null) — the lead's share, the business
      developer's commission, and, on a project with no priced tasks, the whole
      expert share. Written when the client approves delivery.
    * **Task-level** (``task`` is set) — one expert's payment for one approved
      task. Written when the delivery lead approves that task, which can happen
      well before the project itself completes.

    Either way a row means money actually earned; anything still in flight is
    projected on the fly instead, never written here.
    """

    class Kind(models.TextChoices):
        EXPERT = "expert", "Expert share"
        DELIVERY_LEAD = "delivery_lead", "Delivery lead share"
        BUSINESS_DEV = "business_dev", "Business developer commission"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="earnings"
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="earnings")
    # Set on an expert's payment for a single approved task; null on the
    # project-level shares. PROTECT, not CASCADE: deleting a task must never
    # quietly delete a payment, which makes a paid task undeletable — correct,
    # because by then it's a financial record rather than a to-do.
    task = models.ForeignKey(
        "projects.Task", on_delete=models.PROTECT,
        null=True, blank=True, related_name="earnings",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Snapshot of the share that produced this amount, so history stays readable
    # after an admin edits the percentages.
    share_percent = models.DecimalField(max_digits=5, decimal_places=2)
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        # Two constraints rather than one, because a NULL `task` can't take part
        # in a plain unique index — every project-level row would look distinct
        # from every other and the "once per role" rule would stop holding.
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user", "kind"],
                condition=models.Q(task__isnull=True),
                name="unique_project_earning_per_role",
            ),
            models.UniqueConstraint(
                fields=["task", "user"],
                condition=models.Q(task__isnull=False),
                name="unique_task_earning_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user} · {self.project.code} · ${self.amount_usd}"


class Withdrawal(models.Model):
    """A payout request against an earner's available balance.

    Requests are settled by bank transfer and then marked paid by a delivery lead
    or an admin — nobody may settle their own request.
    """

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PROCESSING = "processing", "Sending"
        PAID = "paid", "Paid"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    # Money is committed while a payout is queued, in flight, or done. A rejected
    # or failed payout never left, so those amounts go back to the balance.
    COMMITTED_STATUSES = [Status.REQUESTED, Status.PROCESSING, Status.PAID]
    OPEN_STATUSES = [Status.REQUESTED, Status.PROCESSING]
    # Terminal: no further transitions allowed.
    FINAL_STATUSES = [Status.PAID, Status.REJECTED]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="withdrawals"
    )
    reference = models.CharField(max_length=40, unique=True)
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2)
    # What the earner is paid in, converted at request time (kobo / cents).
    currency = models.CharField(max_length=8, default="NGN")
    amount_subunit = models.PositiveBigIntegerField(default=0)
    usd_to_ngn_rate = models.DecimalField(
        "USD → NGN rate used", max_digits=12, decimal_places=2, null=True, blank=True
    )
    # Payout destination, snapshotted from the user's profile at request time.
    bank_name = models.CharField(max_length=120)
    bank_code = models.CharField(max_length=20, blank=True)
    bank_account_number = models.CharField(max_length=34)
    bank_account_name = models.CharField(max_length=150)
    # Paystack transfer trail. Populated when a payout is actually sent.
    recipient_code = models.CharField(max_length=100, blank=True)
    transfer_code = models.CharField(max_length=100, blank=True)
    transfer_reference = models.CharField(max_length=100, blank=True)
    transfer_raw = models.JSONField(default=dict, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    note = models.TextField(blank=True, help_text="Transfer reference, or why it was rejected.")
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="processed_withdrawals",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.reference} · {self.user} · ${self.amount_usd} · {self.status}"

    @property
    def masked_account(self):
        """Last four digits only — enough to recognise the account in a list."""
        tail = self.bank_account_number[-4:]
        return f"•••• {tail}" if tail else ""


class Refund(models.Model):
    """Money going back to a client.

    The counterpart to `Earning`: that ledger records value flowing out to the
    people who delivered, this one records it flowing back to the person who
    paid. Both are append-only, and neither is ever recalculated from current
    settings — a refund is a fact about a moment.

    The hard rule this model exists to keep: **no expert is ever debited.** A
    refund is funded first from money the platform still holds, then from the
    reserve, and finally out of the platform's own pocket. Nobody who has been
    paid for approved work is asked to give it back, because "the money is
    already in the building" is the promise the whole delivery model rests on.
    """

    class Status(models.TextChoices):
        REQUESTED = "requested", "Awaiting approval"
        APPROVED = "approved", "Approved"
        PROCESSED = "processed", "Refunded"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    # Statuses where the money is considered gone from the platform's side.
    # A rejected or failed refund never left, so it doesn't reduce what is
    # still refundable.
    SETTLED_STATUSES = [Status.PROCESSED]
    OPEN_STATUSES = [Status.REQUESTED, Status.APPROVED]

    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="refunds"
    )
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2)
    # Always required. A refund with no recorded reason is indistinguishable
    # from a mistake when someone reads the books six months later.
    reason = models.TextField()
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.REQUESTED
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="refunds_requested",
    )
    # Null while awaiting a decision, and on refunds a lead could issue unaided.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="refunds_approved",
    )
    # How much of this refund the platform still held, versus what had to come
    # out of the reserve, versus what the platform absorbed because the reserve
    # was short. Snapshotted rather than derived, because all three move as
    # later refunds land.
    funded_from_held_usd = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )
    funded_from_reserve_usd = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )
    absorbed_usd = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )
    gateway = models.CharField(max_length=20, blank=True)
    gateway_reference = models.CharField(max_length=120, blank=True)
    gateway_raw = models.JSONField(default=dict, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    # Set when a refund was settled outside the platform — the same escape
    # hatch withdrawals already have, because money sometimes moves by bank
    # transfer and the books still have to agree.
    settled_manually = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.project.code} · refund ${self.amount_usd} · {self.status}"

    @property
    def is_settled(self):
        return self.status in self.SETTLED_STATUSES


class ReserveEntry(models.Model):
    """The platform's refund reserve, as a ledger rather than a number.

    A slice of the platform's own remainder is earmarked on every completed
    project, and refunds that outrun what a project still holds draw against
    it. Rows rather than a running total for the same reason `Earning` is a
    ledger: a balance you can't explain is a balance nobody trusts, and this
    one has to be explainable to whoever asks why a refund was affordable.

    Note this is an *earmark*, not a fourth share. It comes out of what the
    platform already keeps, so it never changes what an expert, a lead or a
    business developer is paid.
    """

    class Kind(models.TextChoices):
        CONTRIBUTION = "contribution", "Set aside"
        DRAW = "draw", "Drawn for a refund"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    # Positive on both kinds; the sign is the kind's job. Mixing signed amounts
    # with a kind field is how ledgers end up double-negating.
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2)
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="reserve_entries"
    )
    refund = models.ForeignKey(
        Refund, on_delete=models.PROTECT, null=True, blank=True,
        related_name="reserve_entries",
    )
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "reserve entries"
        constraints = [
            # One set-aside per project, ever. Crediting runs lazily on every
            # earnings read (`backfill`), so without this the reserve would
            # grow every time somebody opened their earnings page.
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(kind="contribution"),
                name="unique_reserve_contribution_per_project",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} ${self.amount_usd} · {self.project.code}"
