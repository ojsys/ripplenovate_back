import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import DatabaseError, models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Manager for the email-as-username custom user."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("An email address is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", User.Role.DELIVERY_LEAD)
        extra.setdefault("is_email_verified", True)
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Custom user: logs in with email, carries a platform role."""

    class Role(models.TextChoices):
        CLIENT = "client", "Client"
        DELIVERY_LEAD = "delivery_lead", "Delivery Lead"
        DEVELOPER = "developer", "Developer"

    # Drop the username field — email is the identifier.
    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CLIENT)

    # Client-specific
    company = models.CharField(max_length=150, blank=True)
    # Developer-specific
    specialty = models.CharField(max_length=150, blank=True)
    active_load = models.PositiveIntegerField(default=0)

    # Payout account for earners (developers and delivery leads). Snapshotted onto
    # every Withdrawal so a later edit never rewrites past payout history.
    bank_name = models.CharField(max_length=120, blank=True)
    # Paystack's bank identifier — required to create a transfer recipient, so a
    # payout can only be sent once this is set (not just a typed bank name).
    bank_code = models.CharField(max_length=20, blank=True)
    bank_account_number = models.CharField(max_length=34, blank=True)
    bank_account_name = models.CharField(max_length=150, blank=True)
    # Paystack transfer recipient, reused across payouts for the same account.
    paystack_recipient_code = models.CharField(max_length=100, blank=True)

    is_email_verified = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return f"{self.full_name or self.email} ({self.role})"

    @property
    def initials(self):
        source = self.full_name.strip() or self.email
        parts = source.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return source[:2].upper()

    @property
    def role_label(self):
        if self.role == self.Role.DELIVERY_LEAD:
            return "Delivery Lead"
        return self.get_role_display()

    @property
    def can_earn(self):
        """Developers and delivery leads are paid out of delivered project value."""
        return self.role in (self.Role.DEVELOPER, self.Role.DELIVERY_LEAD)

    @property
    def has_payout_account(self):
        """Enough detail to actually send money — a bank code, not just a name."""
        return bool(self.bank_code and self.bank_account_number and self.bank_account_name)


class EmailToken(models.Model):
    """One-time token for email verification and password reset."""

    class Purpose(models.TextChoices):
        VERIFY = "verify", "Email verification"
        RESET = "reset", "Password reset"

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_tokens")
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    TTL = timedelta(hours=24)

    def is_valid(self):
        return self.used_at is None and timezone.now() - self.created_at < self.TTL

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])


class SiteSettings(models.Model):
    """Editable site-wide branding — a single row managed in the Django admin."""

    # Fallbacks used before the row exists (and by the payout math if the table
    # hasn't been migrated yet).
    DEFAULT_DEVELOPER_SHARE = Decimal("60.00")
    DEFAULT_LEAD_SHARE = Decimal("15.00")
    DEFAULT_MIN_WITHDRAWAL = Decimal("50.00")

    brand_name = models.CharField(max_length=100, default="Ripple Innovation Labs")
    tagline = models.CharField(max_length=150, default="Work Globally · Thrive Locally")
    usd_to_ngn_rate = models.DecimalField(
        "USD → NGN rate",
        max_digits=12,
        decimal_places=2,
        default=Decimal("1600.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Naira per 1 USD. Applied to every invoice charged in NGN from the "
                  "moment it's saved — update it when the market rate moves.",
    )
    developer_share_percent = models.DecimalField(
        "Developer share of a quote (%)",
        max_digits=5,
        decimal_places=2,
        default=DEFAULT_DEVELOPER_SHARE,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="What the assigned developer earns from a project's quote once the "
                  "client approves delivery. Applies to projects completed from now on.",
    )
    delivery_lead_share_percent = models.DecimalField(
        "Delivery lead share of a quote (%)",
        max_digits=5,
        decimal_places=2,
        default=DEFAULT_LEAD_SHARE,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="What the delivery lead who quoted the project earns on approval. "
                  "The remainder of the quote stays with the platform.",
    )
    min_withdrawal_usd = models.DecimalField(
        "Minimum withdrawal (USD)",
        max_digits=10,
        decimal_places=2,
        default=DEFAULT_MIN_WITHDRAWAL,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="The smallest balance a developer or delivery lead can withdraw.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return self.brand_name

    @property
    def platform_share_percent(self):
        """What's left of a quote after the developer and delivery lead take theirs.

        Derived rather than stored so the three shares can never fail to add up.
        """
        remainder = (Decimal("100")
                     - (self.developer_share_percent or Decimal("0"))
                     - (self.delivery_lead_share_percent or Decimal("0")))
        return max(remainder, Decimal("0"))

    def clean(self):
        shares = self.developer_share_percent + self.delivery_lead_share_percent
        if shares > Decimal("100"):
            raise ValidationError(
                "The developer and delivery lead shares can't add up to more than "
                f"100% of a quote (currently {shares}%)."
            )

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce a single row (singleton)
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def usd_to_ngn(cls):
        """Live USD→NGN rate: the admin-editable value, else the .env fallback.

        Read-only (no row is created) so it's safe on the payment path.
        """
        try:
            rate = cls.objects.values_list("usd_to_ngn_rate", flat=True).first()
        except DatabaseError:  # table not migrated yet
            rate = None
        if rate and rate > 0:
            return Decimal(rate)
        return Decimal(str(settings.USD_TO_NGN_RATE))

    @classmethod
    def payout_config(cls):
        """Payout percentages + withdrawal floor, with defaults if no row exists.

        Read-only (no row is created) so it's safe on the earnings path.
        """
        fields = ("developer_share_percent", "delivery_lead_share_percent",
                  "min_withdrawal_usd")
        try:
            row = cls.objects.values(*fields).first()
        except DatabaseError:  # table not migrated yet
            row = None
        row = row or {}
        # A configured 0% is meaningful, so only fall back on a missing value.
        def pick(key, default):
            value = row.get(key)
            return Decimal(value) if value is not None else default

        return {
            "developer_share_percent": pick("developer_share_percent",
                                            cls.DEFAULT_DEVELOPER_SHARE),
            "delivery_lead_share_percent": pick("delivery_lead_share_percent",
                                                cls.DEFAULT_LEAD_SHARE),
            "min_withdrawal_usd": pick("min_withdrawal_usd", cls.DEFAULT_MIN_WITHDRAWAL),
        }
