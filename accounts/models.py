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

    # Whether this partner appears on the public leaderboard. Opt-out rather
    # than opt-in: being listed is part of delivering on a platform that sells
    # its people, and an empty leaderboard helps nobody. But somebody who would
    # rather not be ranked in public gets to say so.
    show_in_leaderboard = models.BooleanField(
        "Show on the public leaderboard", default=True,
        help_text="Delivery leads and experts with enough delivered work appear "
                  "on the public leaderboard. Untick to be left off it.",
    )

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
        fields = ["approval_status", "approved_at", "approved_by", "rejection_reason"]
        # An admin can approve someone who never submitted through the wizard —
        # from the Django admin, or ahead of them finishing. Leaving onboarding
        # "incomplete" then traps an approved person in the signup flow, so
        # approval closes it out.
        if not self.onboarding_completed_at:
            self.onboarding_completed_at = timezone.now()
            fields.append("onboarding_completed_at")
        self.save(update_fields=fields)

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


class Notification(models.Model):
    """One thing that happened, waiting in someone's bell.

    Written by `send_brand_email` rather than by each caller, deliberately. The
    two channels then share one recipient list by construction — there is no
    way to add a notification that emails somebody and doesn't reach their
    bell, or the reverse, because there is only one place either happens.

    That matters more than it sounds. Sixteen notification types previously
    terminated in SMTP alone, which made email deliverability the product's
    uptime: one spam classification and a client silently stopped responding —
    now the exact condition under which a lead may close a project over their
    head.

    Deliberately dumb. No types, no grouping, no preferences. A title, a line
    of body, and somewhere to go. Those can be added when there is evidence
    anybody wants them.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="notifications",
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    # Where the bell takes you. Stored as a path rather than a FK so this model
    # stays free of every app that might want to notify from one.
    url = models.CharField(max_length=300, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            # The unread count runs on every /auth/me, which is every page load.
            models.Index(fields=["user", "read_at"]),
        ]

    def __str__(self):
        return f"{self.user_id} · {self.title}"


class ImpersonationEvent(models.Model):
    """A record that an admin signed in as somebody else.

    Impersonation makes one person's actions indistinguishable from another's,
    which is exactly what makes it useful for support and exactly what makes it
    dangerous. The row is written before the token is handed over, so a session
    cannot happen without a trace of who opened it, against whom, and from
    where — and `ended_at` says whether they stepped back out or just let it
    lapse.
    """

    impersonator = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="impersonations_started",
        help_text="The admin who started the session.",
    )
    target = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="impersonations_received",
        help_text="The account they signed in as.",
    )
    reason = models.CharField(
        max_length=200, blank=True,
        help_text="What the admin said they were doing.",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    # Set when they hand the session back. Null means it was abandoned rather
    # than closed — the token simply expired.
    ended_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.impersonator_id} as {self.target_id} at {self.started_at}"


class SiteSettings(models.Model):
    """Editable site-wide branding — a single row managed in the Django admin."""

    # Fallbacks used before the row exists (and by the payout math if the table
    # hasn't been migrated yet).
    # The people who do the work take the largest share by a wide margin. The
    # platform's cut is whatever the other three don't claim — 15% on a direct
    # project, 10% once a business developer's commission comes out of it.
    DEFAULT_EXPERT_SHARE = Decimal("70.00")
    DEFAULT_LEAD_SHARE = Decimal("15.00")
    DEFAULT_BUSINESS_DEV_SHARE = Decimal("5.00")
    DEFAULT_MIN_WITHDRAWAL = Decimal("50.00")
    # A working week plus a weekend. Long enough that a client on leave isn't
    # closed out behind their back, short enough that a team isn't left unpaid
    # by someone who has simply stopped replying.
    DEFAULT_CLIENT_SILENCE_DAYS = 7
    # Small enough not to matter on a good month, large enough to cover the
    # occasional failed project without reaching into working capital.
    DEFAULT_RESERVE_PERCENT = Decimal("5.00")
    DEFAULT_REFUND_THRESHOLD = Decimal("500.00")

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
    reserve_percent = models.DecimalField(
        "Refund reserve (% of the platform's share)",
        max_digits=5,
        decimal_places=2,
        default=DEFAULT_RESERVE_PERCENT,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Set aside from the platform's own share of each completed "
                  "project, to fund refunds on work that has already been paid "
                  "out. This never changes what an expert, delivery lead or "
                  "business developer earns — it comes out of the remainder.",
    )
    refund_admin_threshold_usd = models.DecimalField(
        "Refund needing admin approval (USD)",
        max_digits=10,
        decimal_places=2,
        default=DEFAULT_REFUND_THRESHOLD,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="A delivery lead can issue refunds up to this amount on their "
                  "own projects. Anything larger waits for an administrator.",
    )
    client_silence_days = models.PositiveIntegerField(
        "Days before a lead may close a silent client's project",
        default=DEFAULT_CLIENT_SILENCE_DAYS,
        help_text="How long after reminding a client that a delivery lead may "
                  "complete a project on their behalf and release the team's "
                  "earnings. The clock starts at the first reminder and resets "
                  "if the client says anything. A second delivery lead can "
                  "countersign instead of waiting.",
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


class Organisation(models.Model):
    """A company that buys work, as an entity rather than a string.

    `User.company` was free text, which meant one client meant one login. Any
    real B2B buyer has a procurement contact, a project owner and a budget
    holder, and all three need to see the work — so the free-text field made
    every multi-person buyer either share a password or post their briefs from
    three unconnected accounts.

    Every client belongs to exactly one organisation, including the sole trader
    who has never heard the word: the migration gives them a personal one. One
    code path afterwards, rather than "an org, or else the old behaviour".
    """

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True)
    # Where invoices go when it isn't the person who posted the brief. Blank
    # means "whoever posted it", which is the common case.
    billing_email = models.EmailField(blank=True)
    # What this buyer wants to be charged in. Quotes are stored in USD
    # regardless — this only decides the currency on the card, and with it
    # which rail can carry the charge. Blank means the platform default.
    preferred_currency = models.CharField(
        max_length=3, blank=True,
        help_text="e.g. USD, GBP, EUR, NGN. Blank uses the platform default. "
                  "Quotes are always agreed in USD; this is only what the "
                  "client's card is charged in.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def owner_emails(self):
        return [m.user.email for m in self.memberships.all()
                if m.role == OrganisationMember.Role.OWNER]

    @classmethod
    def ensure_for(cls, user):
        """The organisation this client buys through, creating it if needed.

        Called when a client signs up. Matches an existing company on a
        case-insensitive, whitespace-collapsed name so "Acme Ltd" and "acme
        ltd" land in the same place — and matches on nothing cleverer than
        that, because wrongly merging two real companies would show one buyer
        another's briefs and budgets.

        A client with no company named gets a personal organisation. Odd to
        look at, much better to program against: one shape everywhere instead
        of "an org, or else the old behaviour".
        """
        import re

        from django.utils.text import slugify

        existing = user.organisation_memberships.first()
        if existing:
            return existing.organisation

        typed = (user.company or "").strip()
        if typed:
            key = re.sub(r"\s+", " ", typed).lower()
            org = next(
                (o for o in cls.objects.all()
                 if re.sub(r"\s+", " ", o.name.strip()).lower() == key),
                None,
            )
        else:
            org = None
            typed = user.full_name.strip() or user.email

        if org is None:
            base = slugify(typed)[:150] or "org"
            slug, n = base, 2
            while cls.objects.filter(slug=slug).exists():
                slug = f"{base}-{n}"
                n += 1
            org = cls.objects.create(name=typed, slug=slug)
            role = OrganisationMember.Role.OWNER
        else:
            # Joining a company somebody else already registered. A member, not
            # an owner: matching a typed company name is not proof of authority
            # over it, and an owner can promote them.
            role = OrganisationMember.Role.MEMBER

        OrganisationMember.objects.get_or_create(
            organisation=org, user=user, defaults={"role": role}
        )
        return org


class OrganisationMember(models.Model):
    """Somebody's seat at a client organisation.

    Three roles, and the distinction that matters is **billing**: a finance
    contact needs to see what things cost and nothing else. Handing them the
    brief, the deliverables and the team's progress updates because they have
    to pay an invoice is the sort of over-sharing that stops a buyer rolling
    the platform out past one team.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MEMBER = "member", "Member"
        BILLING = "billing", "Billing only"

    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="organisation_memberships",
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="organisation_invites_sent",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["organisation__name", "user__full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organisation", "user"], name="unique_org_membership"
            ),
        ]

    def __str__(self):
        return f"{self.user} · {self.organisation} ({self.role})"

    @property
    def sees_delivery(self):
        """Whether this seat sees the work itself, or only what it cost."""
        return self.role != self.Role.BILLING


class TermsAcceptance(models.Model):
    """A record that somebody agreed to the terms, and which version.

    The platform's structural defence against people taking the relationship
    off-platform is genuinely strong — a client never chooses their expert,
    often doesn't know who did the work, and the relationship belongs to the
    delivery lead. But "stronger than a marketplace's" isn't "handled", and
    there was no stated position at all.

    Versioned, because terms change and "they agreed" is worthless without
    "to what". Re-prompting on a new version is the point of storing it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="terms_acceptances",
    )
    version = models.CharField(max_length=20)
    accepted_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-accepted_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "version"], name="unique_terms_acceptance"
            ),
        ]

    def __str__(self):
        return f"{self.user_id} accepted {self.version}"
