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
