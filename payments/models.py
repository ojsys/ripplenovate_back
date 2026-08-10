from django.conf import settings
from django.db import models

from projects.models import Project


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="payments")
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
