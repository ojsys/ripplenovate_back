from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from rest_framework import serializers

from catalog.serializers import ProductLineBriefSerializer

from .models import Invitation, KycProfile, ProfessionalProfile, User


class UserSerializer(serializers.ModelSerializer):
    initials = serializers.CharField(read_only=True)
    role_label = serializers.CharField(read_only=True)
    product_lines = ProductLineBriefSerializer(many=True, read_only=True)
    is_approved = serializers.BooleanField(read_only=True)
    needs_approval = serializers.BooleanField(read_only=True)
    onboarding_complete = serializers.SerializerMethodField()
    # Admins carry the delivery_lead role (see `create_superuser`), so `role`
    # alone can't tell one apart from an ordinary lead — which is how
    # admin-only screens ended up in every lead's menu. This is the only honest
    # signal the frontend has.
    is_admin = serializers.BooleanField(source="is_superuser", read_only=True)
    # How much this expert already has on. `active_load` is a stored number a
    # seed once wrote and nothing has maintained since, so it can't answer
    # "can they take this on?" — this counts the live projects they're actually
    # on. The old field stays for now so nothing reading it breaks.
    active_projects = serializers.SerializerMethodField()
    # Whether they're on the roster of whoever is asking. A lead staffs a brief
    # from their own people first, but isn't limited to them — an expert can
    # work across teams if they have the capacity.
    on_my_roster = serializers.SerializerMethodField()

    def get_active_projects(self, obj):
        from projects.models import Project

        if obj.role != User.Role.EXPERT:
            return 0
        return (Project.objects
                .filter(Q(experts=obj) | Q(expert=obj),
                        stage__in=[Project.Stage.PAID, Project.Stage.IN_PROGRESS,
                                   Project.Stage.REVIEW])
                .distinct()
                .count())

    def get_on_my_roster(self, obj):
        viewer = self.context.get("viewer")
        return bool(viewer and obj.lead_id == viewer.id)

    def get_onboarding_complete(self, obj):
        return obj.onboarding_completed_at is not None

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "role_label",
            "company", "specialty", "active_load", "skills", "product_lines",
            "is_email_verified", "initials",
            "approval_status", "is_approved", "needs_approval", "is_admin",
            "active_projects", "on_my_roster",
            "onboarding_step", "onboarding_complete", "rejection_reason",
            "referral_code",
        ]
        read_only_fields = ["id", "role", "is_email_verified", "active_load",
                            "product_lines", "approval_status", "referral_code",
                            "rejection_reason"]


class RegisterSerializer(serializers.ModelSerializer):
    """Public signup. Three doors: client, delivery lead, business developer.

    Experts are deliberately not an option — they come in by invitation from a
    delivery lead, which is what makes a lead's roster something they vouch for.
    """

    password = serializers.CharField(write_only=True, validators=[validate_password])
    # A business developer's shareable code, from /register?ref=CODE.
    referral_code = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    role = serializers.ChoiceField(
        choices=[(r, r.label) for r in User.SELF_SERVE_ROLES],
        default=User.Role.CLIENT,
    )

    class Meta:
        model = User
        fields = ["email", "full_name", "company", "password", "referral_code", "role"]

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        # An unrecognised code is ignored rather than rejected — a mistyped or
        # stale referral link shouldn't block someone from signing up.
        code = (validated_data.pop("referral_code", "") or "").strip().upper()
        referrer = (
            User.objects.filter(
                referral_code=code, role=User.Role.BUSINESS_DEV
            ).first()
            if code else None
        )
        role = validated_data.pop("role", User.Role.CLIENT)
        # Partners start in review and stay there until an admin approves them;
        # a client is live as soon as they verify their email.
        approval = (User.ApprovalStatus.PENDING if role in User.APPROVAL_ROLES
                    else User.ApprovalStatus.NOT_REQUIRED)
        user = User.objects.create_user(
            role=role, referred_by=referrer, approval_status=approval,
            **validated_data
        )
        if role in User.APPROVAL_ROLES:
            ProfessionalProfile.objects.create(user=user)
        if role == User.Role.BUSINESS_DEV:
            user.ensure_referral_code()
        return user


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """A user editing their own profile."""

    class Meta:
        model = User
        fields = ["full_name", "company", "specialty"]

    def validate_full_name(self, v):
        if not v.strip():
            raise serializers.ValidationError("Your name can't be empty.")
        return v.strip()


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField(validators=[validate_password])


class ExpertCreateSerializer(serializers.ModelSerializer):
    """A delivery lead creating an expert account + profile."""

    password = serializers.CharField(write_only=True, validators=[validate_password])

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "specialty", "password"]

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        # Lead-created experts are active immediately (no email verification step).
        return User.objects.create_user(
            role=User.Role.EXPERT, is_email_verified=True, **validated_data
        )


class ExpertUpdateSerializer(serializers.ModelSerializer):
    """A delivery lead editing an expert's profile."""

    class Meta:
        model = User
        fields = ["full_name", "specialty", "active_load", "skills"]


class RoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=User.Role.choices)
    specialty = serializers.CharField(required=False, allow_blank=True)


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    password = serializers.CharField(validators=[validate_password])


class ProfessionalProfileSerializer(serializers.ModelSerializer):
    """The application a delivery lead or business developer fills in."""

    class Meta:
        model = ProfessionalProfile
        fields = ["bio", "country", "timezone_name", "years_experience",
                  "linkedin_url", "portfolio_url", "team_size_target",
                  "past_delivery", "references_note"]
        extra_kwargs = {field: {"required": False} for field in fields}


class OnboardingSerializer(serializers.Serializer):
    """One step of the signup wizard.

    Everything is optional because the wizard saves as it goes — a half-finished
    step must be storable, or closing the tab loses work. Completeness is only
    enforced when the application is submitted for review.
    """

    full_name = serializers.CharField(required=False, allow_blank=True)
    company = serializers.CharField(required=False, allow_blank=True)
    specialty = serializers.CharField(required=False, allow_blank=True)
    skills = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    product_lines = serializers.ListField(
        child=serializers.SlugField(), required=False
    )
    onboarding_step = serializers.IntegerField(required=False, min_value=0)
    profile = ProfessionalProfileSerializer(required=False)


class InvitationSerializer(serializers.ModelSerializer):
    product_lines = ProductLineBriefSerializer(many=True, read_only=True)
    invited_by_name = serializers.SerializerMethodField()
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invitation
        fields = ["id", "email", "full_name", "specialty", "skills",
                  "product_lines", "status", "is_expired", "created_at",
                  "expires_at", "invited_by_name"]

    def get_invited_by_name(self, obj):
        return obj.invited_by.full_name or obj.invited_by.email


class InvitationCreateSerializer(serializers.Serializer):
    """A lead inviting one expert. The view accepts a list of these."""

    email = serializers.EmailField()
    full_name = serializers.CharField(required=False, allow_blank=True)
    specialty = serializers.CharField(required=False, allow_blank=True)
    skills = serializers.ListField(child=serializers.CharField(), required=False)
    product_lines = serializers.ListField(child=serializers.SlugField(), required=False)

    def validate_email(self, value):
        """Only refuse what genuinely can't be done.

        An existing *expert* is fine — the view puts them on the caller's team
        instead of sending an invitation, which is what the lead wanted anyway.
        Inviting someone is "get this person onto my team"; whether that needs
        a new account is an implementation detail they shouldn't have to know,
        and making it their problem sent them looking for an admin.

        A client or a business developer is a different matter: an invitation
        can't turn one into an expert, and quietly reassigning somebody's role
        from a text box is not something this should do.
        """
        value = value.lower().strip()
        existing = User.objects.filter(email__iexact=value).first()
        if existing and existing.role != User.Role.EXPERT:
            raise serializers.ValidationError(
                f"{existing.full_name or value} already has an account here as a "
                f"{existing.get_role_display().lower()}. An admin can change their "
                "role to expert if that's what you meant."
            )
        return value


class InvitationAcceptSerializer(serializers.Serializer):
    """The invitee choosing their own password — the lead never sees it."""

    password = serializers.CharField(validators=[validate_password])
    full_name = serializers.CharField(required=False, allow_blank=True)
    specialty = serializers.CharField(required=False, allow_blank=True)


class ApprovalDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "reject"])
    reason = serializers.CharField(required=False, allow_blank=True)


class ProfessionalProfileDetailSerializer(serializers.ModelSerializer):
    """The professional half of a profile — safe for a delivery lead to read."""

    cv_filename = serializers.CharField(read_only=True)
    has_cv = serializers.SerializerMethodField()

    class Meta:
        model = ProfessionalProfile
        fields = ["bio", "country", "timezone_name", "years_experience",
                  "linkedin_url", "portfolio_url", "team_size_target",
                  "past_delivery", "references_note",
                  "languages", "certifications", "availability_hours",
                  "has_cv", "cv_filename", "cv_uploaded_at"]
        read_only_fields = ["has_cv", "cv_filename", "cv_uploaded_at"]
        extra_kwargs = {
            field: {"required": False}
            for field in ["bio", "country", "timezone_name", "years_experience",
                          "linkedin_url", "portfolio_url", "team_size_target",
                          "past_delivery", "references_note", "languages",
                          "certifications", "availability_hours"]
        }

    def get_has_cv(self, obj):
        return bool(obj.cv)


class KycSerializer(serializers.ModelSerializer):
    """Identity details.

    `id_number` is write-only: it goes in, and comes back only as
    `id_number_masked`. Even the owner reading their own record gets the mask —
    they know their own passport number, and not echoing it means a shoulder-surf
    or a cached response never exposes it.
    """

    id_number = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=60
    )
    id_number_masked = serializers.CharField(
        source="masked_id_number", read_only=True
    )
    has_id_document = serializers.SerializerMethodField()
    missing_fields = serializers.ListField(read_only=True)
    is_complete = serializers.BooleanField(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = KycProfile
        fields = ["legal_name", "date_of_birth", "phone",
                  "address_line1", "address_line2", "city", "state",
                  "postal_code", "country", "id_type", "id_number",
                  "id_number_masked", "tax_id", "has_id_document",
                  "status", "status_label", "submitted_at", "reviewed_at",
                  "rejection_reason", "missing_fields", "is_complete"]
        read_only_fields = ["status", "status_label", "submitted_at", "reviewed_at",
                            "rejection_reason", "missing_fields", "is_complete",
                            "id_number_masked", "has_id_document"]
        extra_kwargs = {
            field: {"required": False}
            for field in ["legal_name", "date_of_birth", "phone", "address_line1",
                          "address_line2", "city", "state", "postal_code",
                          "country", "id_type", "tax_id"]
        }

    def get_has_id_document(self, obj):
        return bool(obj.id_document)


class KycReviewSerializer(KycSerializer):
    """What an admin reviewing a submission sees — the person, plus their record.

    Still no raw ID number: the reviewer compares the uploaded document against
    the typed details, and the document is what carries the number.
    """

    user_id = serializers.IntegerField(source="user.id", read_only=True)
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_role = serializers.CharField(source="user.role_label", read_only=True)

    class Meta(KycSerializer.Meta):
        fields = KycSerializer.Meta.fields + [
            "user_id", "user_name", "user_email", "user_role",
        ]

    def get_user_name(self, obj):
        return obj.user.full_name or obj.user.email


class KycDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["verify", "reject"])
    reason = serializers.CharField(required=False, allow_blank=True)
