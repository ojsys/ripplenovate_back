from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from catalog.serializers import ProductLineBriefSerializer

from .models import Invitation, PartnerProfile, User


class UserSerializer(serializers.ModelSerializer):
    initials = serializers.CharField(read_only=True)
    role_label = serializers.CharField(read_only=True)
    product_lines = ProductLineBriefSerializer(many=True, read_only=True)
    is_approved = serializers.BooleanField(read_only=True)
    needs_approval = serializers.BooleanField(read_only=True)
    onboarding_complete = serializers.SerializerMethodField()

    def get_onboarding_complete(self, obj):
        return obj.onboarding_completed_at is not None

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "role_label",
            "company", "specialty", "active_load", "skills", "product_lines",
            "is_email_verified", "initials",
            "approval_status", "is_approved", "needs_approval",
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
            PartnerProfile.objects.create(user=user)
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


class PartnerProfileSerializer(serializers.ModelSerializer):
    """The application a delivery lead or business developer fills in."""

    class Meta:
        model = PartnerProfile
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
    profile = PartnerProfileSerializer(required=False)


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
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "Someone with this email already has an account."
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
