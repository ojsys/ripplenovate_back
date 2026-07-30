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
    """A realized share of a delivered project, credited to a developer or lead.

    One row per (project, earner, role). Rows are only written once the client
    approves delivery, so the sum of a user's earnings is money actually earned —
    anything still in flight is projected on the fly instead.
    """

    class Kind(models.TextChoices):
        DEVELOPER = "developer", "Developer share"
        DELIVERY_LEAD = "delivery_lead", "Delivery lead share"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="earnings"
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="earnings")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Snapshot of the share that produced this amount, so history stays readable
    # after an admin edits the percentages.
    share_percent = models.DecimalField(max_digits=5, decimal_places=2)
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user", "kind"], name="unique_earning_per_role"
            )
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
        PROCESSING = "processing", "Processing"
        PAID = "paid", "Paid"
        REJECTED = "rejected", "Rejected"

    # Anything but a rejection is money committed against the balance.
    COMMITTED_STATUSES = [Status.REQUESTED, Status.PROCESSING, Status.PAID]
    OPEN_STATUSES = [Status.REQUESTED, Status.PROCESSING]

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
    bank_account_number = models.CharField(max_length=34)
    bank_account_name = models.CharField(max_length=150)
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
