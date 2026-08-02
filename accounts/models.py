import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import DatabaseError, models
from django.utils import timezone

from .uploads import cv_path, id_document_path, validate_cv, validate_id_document


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
        EXPERT = "expert", "Project Delivery Expert"
        BUSINESS_DEV = "business_dev", "Business Developer"

    # Short names for the UI — the full choice labels are too long for a chip in
    # an activity feed or a header.
    SHORT_ROLE_LABELS = {
        Role.CLIENT: "Client",
        Role.DELIVERY_LEAD: "Delivery Lead",
        Role.EXPERT: "Expert",
        Role.BUSINESS_DEV: "Business Developer",
    }

    # Roles a person may pick for themselves at signup. Experts are absent on
    # purpose: they arrive by invitation from a delivery lead, which is what
    # keeps a lead's roster something they actually vouch for.
    SELF_SERVE_ROLES = [Role.CLIENT, Role.DELIVERY_LEAD, Role.BUSINESS_DEV]
    # Roles whose account is reviewed before it can quote work or be paid.
    APPROVAL_ROLES = [Role.DELIVERY_LEAD, Role.BUSINESS_DEV]

    class ApprovalStatus(models.TextChoices):
        NOT_REQUIRED = "n/a", "Not required"
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Not approved"

    # Drop the username field — email is the identifier.
    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CLIENT)

    # Client-specific
    company = models.CharField(max_length=150, blank=True)
    # Expert-specific: a one-line headline ("Product designer · Figma"). Finer
    # skills live in `skills`, and the disciplines they work in are product lines.
    specialty = models.CharField(max_length=150, blank=True)
    active_load = models.PositiveIntegerField(default=0)

    # Which disciplines this person works in. For an expert, the lines they can
    # be assigned in; for a delivery lead, the lines they run — a lead only sees
    # briefs in their own lines.
    product_lines = models.ManyToManyField(
        "catalog.ProductLine", blank=True, related_name="members",
        help_text="For a delivery lead, the lines they run. For an expert, the "
                  "lines they can be assigned work in.",
    )
    # Free-form tags within a line — "Figma", "Django", "Power BI".
    skills = models.JSONField(default=list, blank=True)
    # The delivery lead who brought this expert onto the platform. This is the
    # roster relation: `lead.team_members` is "the experts under this lead".
    lead = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="team_members",
        help_text="The delivery lead this expert works under.",
    )

    # --- Business developer ---
    # Their shareable signup code. Only business developers carry one.
    referral_code = models.CharField(
        max_length=20, blank=True, default="", db_index=True,
        help_text="Shareable code — a client signing up at /register?ref=CODE is "
                  "attributed to this business developer.",
    )
    # The business developer who brought this client in. Every project the client
    # posts is attributed to them by default, and carries the commission.
    referred_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="referred_clients",
        help_text="The business developer who referred this client.",
    )

    # Payout account for earners (experts and delivery leads). Snapshotted onto
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

    # --- Application & onboarding (delivery leads and business developers) ---
    approval_status = models.CharField(
        max_length=10, choices=ApprovalStatus.choices,
        default=ApprovalStatus.NOT_REQUIRED,
        help_text="Delivery leads and business developers are reviewed before they "
                  "can quote work or be paid. Clients and invited experts don't "
                  "need approval.",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_partners",
    )
    rejection_reason = models.TextField(
        blank=True, help_text="Shown to the applicant, so write it for them to read.",
    )
    # Cursor into the signup wizard, so closing the tab resumes where they left off.
    onboarding_step = models.PositiveIntegerField(default=0)
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)

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
        return self.SHORT_ROLE_LABELS.get(self.role) or self.get_role_display()

    @property
    def can_earn(self):
        """Who is paid out of delivered project value.

        Experts and delivery leads take a share of the work; business developers
        take a commission on what they source. All three get the earnings page,
        a payout account, and withdrawals — but a partner whose application is
        still pending can't be paid yet.
        """
        earning_role = self.role in (self.Role.EXPERT, self.Role.DELIVERY_LEAD,
                                     self.Role.BUSINESS_DEV)
        return earning_role and self.is_approved

    @property
    def needs_approval(self):
        """Whether this role is reviewed before it can operate."""
        return self.role in self.APPROVAL_ROLES

    # Only these two states withhold access. Everything else — including
    # NOT_REQUIRED — is cleared to operate.
    BLOCKING_STATUSES = [ApprovalStatus.PENDING, ApprovalStatus.REJECTED]

    @property
    def is_approved(self):
        """Cleared to quote work and be paid.

        Read NOT_REQUIRED literally: no review applies, so the account operates.
        That covers clients and invited experts, and equally a lead created from
        the shell, the seed, or the Django admin — those never went through the
        application flow, and silently crippling them would be a nasty trap.

        Only self-serve signups are put into PENDING, and only they are held.
        Superusers are exempt so a fresh install is never locked out of itself.
        """
        if self.is_superuser or not self.needs_approval:
            return True
        return self.approval_status not in self.BLOCKING_STATUSES

    def submit_application(self):
        """Move a finished onboarding into the review queue."""
        self.approval_status = self.ApprovalStatus.PENDING
        self.applied_at = timezone.now()
        self.onboarding_completed_at = timezone.now()
        self.rejection_reason = ""
        self.save(update_fields=["approval_status", "applied_at",
                                 "onboarding_completed_at", "rejection_reason"])

    def approve(self, by=None):
        self.approval_status = self.ApprovalStatus.APPROVED
        self.approved_at = timezone.now()
        self.approved_by = by
        self.rejection_reason = ""
        self.save(update_fields=["approval_status", "approved_at", "approved_by",
                                 "rejection_reason"])

    def reject(self, reason="", by=None):
        self.approval_status = self.ApprovalStatus.REJECTED
        self.approved_by = by
        self.rejection_reason = reason
        self.save(update_fields=["approval_status", "approved_by", "rejection_reason"])

    def ensure_referral_code(self):
        """Give a business developer their shareable code. Idempotent."""
        if self.role != self.Role.BUSINESS_DEV or self.referral_code:
            return self.referral_code
        import secrets
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes
        for _ in range(20):
            code = "RIL-BD-" + "".join(secrets.choice(alphabet) for _ in range(4))
            if not User.objects.filter(referral_code=code).exists():
                self.referral_code = code
                self.save(update_fields=["referral_code"])
                return code
        return ""

    @property
    def has_payout_account(self):
        """Enough detail to actually send money — a bank code, not just a name."""
        return bool(self.bank_code and self.bank_account_number and self.bank_account_name)


class ProfessionalProfile(models.Model):
    """Who someone is professionally — what they do and what they've delivered.

    Shared by everyone who works on the platform: it started as the delivery-lead
    and business-developer application, and experts fill in the same shape of
    information. An admin reviewing an application and a lead deciding who to
    assign are both asking "what can this person do", so it's one model.

    **This is the non-sensitive half.** A delivery lead can read their own
    experts' profiles, because staffing a brief requires it. Identity documents
    live in `KycProfile`, which a lead can never see.
    """

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="professional_profile"
    )
    bio = models.TextField(blank=True)
    country = models.CharField(max_length=80, blank=True)
    timezone_name = models.CharField("Timezone", max_length=64, blank=True)
    years_experience = models.PositiveIntegerField(null=True, blank=True)
    linkedin_url = models.URLField(blank=True)
    portfolio_url = models.URLField(blank=True)
    # Delivery lead only: how big a team they intend to bring.
    team_size_target = models.PositiveIntegerField(null=True, blank=True)
    past_delivery = models.TextField(
        blank=True, help_text="What they've delivered before — the heart of the review.",
    )
    references_note = models.TextField("References", blank=True)

    # --- Expert-facing detail: what a lead needs to staff a brief well ---
    cv = models.FileField(
        "CV / résumé", upload_to=cv_path, blank=True, null=True,
        validators=[validate_cv],
        help_text="PDF or Word document, up to 5MB. Visible to their delivery lead.",
    )
    cv_uploaded_at = models.DateTimeField(null=True, blank=True)
    languages = models.JSONField(
        default=list, blank=True,
        help_text='Languages they work in, e.g. ["English", "French"].',
    )
    certifications = models.TextField(
        blank=True, help_text="Qualifications and certifications, one per line.",
    )
    availability_hours = models.PositiveIntegerField(
        "Availability (hours/week)", null=True, blank=True,
        validators=[MaxValueValidator(168)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} · professional profile"

    @property
    def cv_filename(self):
        """The stored name is a UUID; show something a person can recognise."""
        if not self.cv:
            return ""
        from pathlib import Path as _Path
        return f"CV{_Path(self.cv.name).suffix}"


class KycProfile(models.Model):
    """Identity verification for anyone the platform pays.

    Kept apart from `ProfessionalProfile` on purpose. A delivery lead reads their
    experts' professional profiles to staff work; nobody but the person
    themselves and an admin reviewing verification should ever see a date of
    birth, a home address or a passport number. Separate models make that a
    structural boundary rather than a field-by-field filter somebody forgets to
    apply.

    Deliberately minimal: enough to know who is being paid and that they are who
    they say, and nothing more. No biometrics, no bank statements, no
    next-of-kin.
    """

    class Status(models.TextChoices):
        UNSUBMITTED = "unsubmitted", "Not submitted"
        PENDING = "pending", "Awaiting review"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    class IdType(models.TextChoices):
        PASSPORT = "passport", "Passport"
        NATIONAL_ID = "national_id", "National ID card"
        DRIVERS_LICENCE = "drivers_licence", "Driver's licence"
        VOTERS_CARD = "voters_card", "Voter's card"

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="kyc"
    )
    # The name on the identity document, which is often not the display name.
    legal_name = models.CharField(max_length=150, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=32, blank=True)

    address_line1 = models.CharField("Address", max_length=200, blank=True)
    address_line2 = models.CharField("Address line 2", max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField("State / region", max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=80, blank=True)

    id_type = models.CharField(max_length=20, choices=IdType.choices, blank=True)
    id_number = models.CharField(
        max_length=60, blank=True,
        help_text="Sensitive. Returned masked by the API to everyone except the "
                  "person themselves; never included in list endpoints.",
    )
    id_document = models.FileField(
        "ID document", upload_to=id_document_path, blank=True, null=True,
        validators=[validate_id_document],
    )
    # Tax reference, where the person's jurisdiction issues one.
    tax_id = models.CharField("Tax ID / TIN", max_length=60, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.UNSUBMITTED
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_kyc",
    )
    rejection_reason = models.TextField(
        blank=True, help_text="Shown to the person, so write it for them to read.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "KYC record"
        verbose_name_plural = "KYC records"

    def __str__(self):
        return f"{self.user} · {self.get_status_display()}"

    # Everything needed before a submission can be reviewed at all.
    REQUIRED = ["legal_name", "date_of_birth", "phone", "address_line1",
                "city", "country", "id_type", "id_number"]

    @property
    def missing_fields(self):
        missing = [f for f in self.REQUIRED if not getattr(self, f)]
        if not self.id_document:
            missing.append("id_document")
        return missing

    @property
    def is_complete(self):
        return not self.missing_fields

    @property
    def is_verified(self):
        return self.status == self.Status.VERIFIED

    @property
    def masked_id_number(self):
        """Last four only — enough to confirm which document, useless if leaked."""
        if not self.id_number:
            return ""
        tail = self.id_number[-4:]
        return f"•••• {tail}"

    def submit(self):
        self.status = self.Status.PENDING
        self.submitted_at = timezone.now()
        self.rejection_reason = ""
        self.save(update_fields=["status", "submitted_at", "rejection_reason"])

    def verify(self, by=None):
        self.status = self.Status.VERIFIED
        self.reviewed_at = timezone.now()
        self.reviewed_by = by
        self.rejection_reason = ""
        self.save(update_fields=["status", "reviewed_at", "reviewed_by",
                                 "rejection_reason"])

    def reject(self, reason="", by=None):
        self.status = self.Status.REJECTED
        self.reviewed_at = timezone.now()
        self.reviewed_by = by
        self.rejection_reason = reason
        self.save(update_fields=["status", "reviewed_at", "reviewed_by",
                                 "rejection_reason"])


def _invite_expiry():
    return timezone.now() + Invitation.TTL


class Invitation(models.Model):
    """A delivery lead inviting an expert onto their roster.

    This replaces a lead typing a temporary password on someone else's behalf.
    The invitee sets their own password, which means the lead never handles a
    credential that isn't theirs.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REVOKED = "revoked", "Revoked"

    TTL = timedelta(days=14)

    email = models.EmailField()
    full_name = models.CharField(max_length=150, blank=True)
    specialty = models.CharField(max_length=150, blank=True)
    skills = models.JSONField(default=list, blank=True)
    product_lines = models.ManyToManyField(
        "catalog.ProductLine", blank=True, related_name="invitations"
    )
    invited_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="sent_invitations"
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    expires_at = models.DateTimeField(default=_invite_expiry)
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One live invitation per address. Re-inviting someone whose earlier
            # invitation was revoked or accepted is still fine.
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(status="pending"),
                name="one_pending_invitation_per_email",
            )
        ]

    def __str__(self):
        return f"{self.email} · {self.status}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_open(self):
        return self.status == self.Status.PENDING and not self.is_expired


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
    DEFAULT_EXPERT_SHARE = Decimal("60.00")
    DEFAULT_LEAD_SHARE = Decimal("15.00")
    DEFAULT_BUSINESS_DEV_SHARE = Decimal("5.00")
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
    expert_share_percent = models.DecimalField(
        "Expert share of a quote (%)",
        max_digits=5,
        decimal_places=2,
        default=DEFAULT_EXPERT_SHARE,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="What the assigned project delivery expert earns from a project's "
                  "quote once the client approves delivery. Applies to projects "
                  "completed from now on.",
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
    business_dev_share_percent = models.DecimalField(
        "Business developer commission (%)",
        max_digits=5,
        decimal_places=2,
        default=DEFAULT_BUSINESS_DEV_SHARE,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Commission for the business developer who sourced a project, paid "
                  "on completion. Charged only on projects that have one — a direct "
                  "project keeps this in the platform's share.",
    )
    require_kyc_for_payout = models.BooleanField(
        "Require identity verification before payout",
        default=False,
        help_text="When on, an expert, delivery lead or business developer must be "
                  "KYC-verified before they can withdraw. Left OFF by default so "
                  "turning KYC on never silently strands people who are already "
                  "owed money — switch it on once your team is verified.",
    )
    min_withdrawal_usd = models.DecimalField(
        "Minimum withdrawal (USD)",
        max_digits=10,
        decimal_places=2,
        default=DEFAULT_MIN_WITHDRAWAL,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="The smallest balance an expert or delivery lead can withdraw.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return self.brand_name

    @property
    def platform_share_percent(self):
        """What's left of a quote once everyone else has taken theirs.

        Derived rather than stored so the shares can never fail to add up. This
        is the *worst case* for the platform — it assumes a business developer
        is attributed. A direct project keeps that commission instead.
        """
        remainder = (Decimal("100")
                     - (self.expert_share_percent or Decimal("0"))
                     - (self.delivery_lead_share_percent or Decimal("0"))
                     - (self.business_dev_share_percent or Decimal("0")))
        return max(remainder, Decimal("0"))

    @property
    def platform_share_direct_percent(self):
        """The platform's cut on a project with no business developer."""
        remainder = (Decimal("100")
                     - (self.expert_share_percent or Decimal("0"))
                     - (self.delivery_lead_share_percent or Decimal("0")))
        return max(remainder, Decimal("0"))

    def clean(self):
        shares = (self.expert_share_percent
                  + self.delivery_lead_share_percent
                  + (self.business_dev_share_percent or Decimal("0")))
        if shares > Decimal("100"):
            raise ValidationError(
                "The expert, delivery lead and business developer shares can't add "
                f"up to more than 100% of a quote (currently {shares}%)."
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
        fields = ("expert_share_percent", "delivery_lead_share_percent",
                  "business_dev_share_percent", "min_withdrawal_usd",
                  "require_kyc_for_payout")
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
            "expert_share_percent": pick("expert_share_percent",
                                         cls.DEFAULT_EXPERT_SHARE),
            "delivery_lead_share_percent": pick("delivery_lead_share_percent",
                                                cls.DEFAULT_LEAD_SHARE),
            "business_dev_share_percent": pick("business_dev_share_percent",
                                               cls.DEFAULT_BUSINESS_DEV_SHARE),
            "min_withdrawal_usd": pick("min_withdrawal_usd", cls.DEFAULT_MIN_WITHDRAWAL),
            "require_kyc_for_payout": bool(row.get("require_kyc_for_payout")),
        }
