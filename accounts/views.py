import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import ProductLine

from .emails import (
    notify_admins_of_application,
    notify_admins_of_kyc,
    notify_lead_invitation_accepted,
    send_application_received,
    send_application_rejected,
    send_business_dev_welcome,
    send_delivery_lead_welcome,
    send_expert_invitation,
    send_expert_welcome,
    send_kyc_rejected,
    send_kyc_verified,
    send_password_reset_email,
    send_verification_email,
    send_welcome_client,
)
from .models import (
    EmailToken,
    Invitation,
    KycProfile,
    ProfessionalProfile,
    SiteSettings,
)
from .serializers import (
    ApprovalDecisionSerializer,
    KycDecisionSerializer,
    KycReviewSerializer,
    KycSerializer,
    ProfessionalProfileDetailSerializer,
    ChangePasswordSerializer,
    ExpertCreateSerializer,
    ExpertUpdateSerializer,
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    InvitationSerializer,
    OnboardingSerializer,
    ProfessionalProfileSerializer,
    PasswordResetConfirmSerializer,
    ProfileUpdateSerializer,
    RegisterSerializer,
    RoleUpdateSerializer,
    UserSerializer,
)
from .uploads import validate_cv, validate_id_document

User = get_user_model()


def _require_lead(user, approved=True):
    """Gate an action to a delivery lead.

    `approved=False` for the onboarding surface itself — a lead in review must
    still be able to build their team, since that's part of what's reviewed.
    """
    if user.role != User.Role.DELIVERY_LEAD and not user.is_superuser:
        raise PermissionDenied("Only a delivery lead can do that.")
    if approved and not user.is_approved:
        raise PermissionDenied(
            "Your delivery lead account is still being reviewed."
        )


def tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


@api_view(["GET"])
@permission_classes([AllowAny])
def site_settings(request):
    """Public branding read by the frontend (brand name, tagline)."""
    s = SiteSettings.load()
    return Response({"brand_name": s.brand_name, "tagline": s.tagline})


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    send_verification_email(user)
    return Response(
        {"user": UserSerializer(user).data,
         "detail": "Account created. Check your email to verify your account."},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    email = (request.data.get("email") or "").lower().strip()
    password = request.data.get("password") or ""
    user = User.objects.filter(email__iexact=email).first()
    if not user or not user.check_password(password):
        return Response({"detail": "Invalid email or password."},
                        status=status.HTTP_401_UNAUTHORIZED)
    if not user.is_email_verified:
        return Response(
            {"detail": "Please verify your email before signing in.",
             "code": "email_unverified"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return Response({**tokens_for(user), "user": UserSerializer(user).data})


def _lookup_token(raw, purpose):
    """Fetch a token, treating a malformed/empty UUID as simply 'not found'."""
    try:
        uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None
    return (
        EmailToken.objects.filter(token=raw, purpose=purpose)
        .select_related("user")
        .first()
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_email(request):
    raw = request.data.get("token")
    token = _lookup_token(raw, EmailToken.Purpose.VERIFY)
    if not token or not token.is_valid():
        return Response({"detail": "This verification link is invalid or has expired."},
                        status=status.HTTP_400_BAD_REQUEST)
    user = token.user
    already_verified = user.is_email_verified
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])
    token.mark_used()
    # Send the welcome email the first time a client verifies.
    if not already_verified and user.role == User.Role.CLIENT:
        send_welcome_client(user)
    return Response({**tokens_for(user), "user": UserSerializer(user).data,
                     "detail": "Email verified. You're all set."})


@api_view(["POST"])
@permission_classes([AllowAny])
def resend_verification(request):
    email = (request.data.get("email") or "").lower().strip()
    user = User.objects.filter(email__iexact=email, is_email_verified=False).first()
    if user:
        send_verification_email(user)
    # Always report success so we don't leak which emails exist.
    return Response({"detail": "If that account needs verification, a new link is on its way."})


@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_request(request):
    email = (request.data.get("email") or "").lower().strip()
    user = User.objects.filter(email__iexact=email).first()
    if user:
        send_password_reset_email(user)
    return Response({"detail": "If an account exists for that email, a reset link has been sent."})


@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_confirm(request):
    serializer = PasswordResetConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    token = EmailToken.objects.filter(
        token=serializer.validated_data["token"], purpose=EmailToken.Purpose.RESET
    ).select_related("user").first()
    if not token or not token.is_valid():
        return Response({"detail": "This reset link is invalid or has expired."},
                        status=status.HTTP_400_BAD_REQUEST)
    user = token.user
    user.set_password(serializer.validated_data["password"])
    user.is_email_verified = True  # a successful reset also proves email ownership
    user.save(update_fields=["password", "is_email_verified"])
    token.mark_used()
    return Response({"detail": "Password updated. You can now sign in."})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    """Read or update the signed-in user's own profile."""
    if request.method == "PATCH":
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(UserSerializer(request.user).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Change the signed-in user's password (requires the current one)."""
    serializer = ChangePasswordSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not request.user.check_password(serializer.validated_data["old_password"]):
        return Response({"detail": "Your current password is incorrect."},
                        status=status.HTTP_400_BAD_REQUEST)
    request.user.set_password(serializer.validated_data["new_password"])
    request.user.save(update_fields=["password"])
    return Response({"detail": "Password updated."})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def experts(request):
    """List expert accounts, or (delivery lead) create a new one with a profile.

    Filters:
      ``?mine=1``            only the experts on the caller's own roster
      ``?product_line=slug`` only experts who work in that discipline

    The assignment picker uses both: a lead staffs a brief from their own team,
    in the discipline the brief belongs to — not from a global talent pool.
    """
    if request.method == "POST":
        _require_lead(request.user)
        serializer = ExpertCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # A lead-created expert joins that lead's roster.
        user = serializer.save(lead=request.user)
        slugs = request.data.get("product_lines") or []
        # Default to the lead's own disciplines, so an expert is never created
        # unassignable.
        lines = (ProductLine.objects.filter(slug__in=slugs, is_active=True)
                 if slugs else request.user.product_lines.all())
        user.product_lines.set(lines)
        send_expert_welcome(user)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    qs = User.objects.filter(role=User.Role.EXPERT)
    if request.query_params.get("mine") in ("1", "true"):
        qs = qs.filter(lead=request.user)
    line_slug = request.query_params.get("product_line")
    if line_slug:
        qs = qs.filter(product_lines__slug=line_slug)
    qs = qs.prefetch_related("product_lines").distinct().order_by("full_name")
    return Response(UserSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def business_developers(request):
    """The business developers a lead can credit a project to."""
    _require_lead(request.user)
    qs = User.objects.filter(role=User.Role.BUSINESS_DEV).order_by("full_name")
    return Response(UserSerializer(qs, many=True).data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_expert(request, user_id):
    """Delivery lead edits an expert's profile (name, specialty, load)."""
    _require_lead(request.user)
    target = User.objects.filter(id=user_id, role=User.Role.EXPERT).first()
    if not target:
        return Response({"detail": "Expert not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = ExpertUpdateSerializer(target, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(UserSerializer(target).data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_role(request, user_id):
    """Admin/delivery-lead assigns a platform role to a user."""
    if request.user.role != User.Role.DELIVERY_LEAD and not request.user.is_superuser:
        return Response({"detail": "Only a delivery lead can change roles."},
                        status=status.HTTP_403_FORBIDDEN)
    target = User.objects.filter(id=user_id).first()
    if not target:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = RoleUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    previous_role = target.role
    target.role = serializer.validated_data["role"]
    if "specialty" in serializer.validated_data:
        target.specialty = serializer.validated_data["specialty"]
    target.save(update_fields=["role", "specialty"])
    # Welcome the user into their new role (only on an actual change).
    if target.role != previous_role:
        if target.role == User.Role.DELIVERY_LEAD:
            send_delivery_lead_welcome(target)
        elif target.role == User.Role.EXPERT:
            send_expert_welcome(target)
        elif target.role == User.Role.BUSINESS_DEV:
            # Issued before the email, which includes the referral link.
            target.ensure_referral_code()
            send_business_dev_welcome(target)
    return Response(UserSerializer(target).data)


# ---------------------------------------------------------------------------
# Partner onboarding — the resumable signup wizard for leads and business devs
# ---------------------------------------------------------------------------

@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def onboarding(request):
    """Read or save the signed-in partner's onboarding progress.

    PATCH saves whatever the current step supplied and moves the cursor. It is
    deliberately partial and forgiving: the wizard writes on every step so a
    closed tab never loses work, and completeness is checked at submission
    instead.
    """
    user = request.user
    if not user.needs_approval:
        raise PermissionDenied("Onboarding is for delivery leads and business developers.")

    profile, _ = ProfessionalProfile.objects.get_or_create(user=user)

    if request.method == "PATCH":
        serializer = OnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        for field in ("full_name", "company", "specialty"):
            if field in data:
                setattr(user, field, data[field])
        if "skills" in data:
            user.skills = data["skills"]
        if "onboarding_step" in data:
            # Only ever moves forward, so revisiting an earlier step to correct
            # something doesn't reset how far they've actually got.
            user.onboarding_step = max(user.onboarding_step, data["onboarding_step"])
        user.save()

        if "product_lines" in data:
            user.product_lines.set(
                ProductLine.objects.filter(
                    slug__in=data["product_lines"], is_active=True
                )
            )
        if "profile" in data:
            for field, value in data["profile"].items():
                setattr(profile, field, value)
            profile.save()

    return Response({
        "user": UserSerializer(user).data,
        "profile": ProfessionalProfileSerializer(profile).data,
        "team_size": user.team_members.count(),
        "pending_invitations": user.sent_invitations.filter(
            status=Invitation.Status.PENDING).count(),
        "has_payout_account": user.has_payout_account,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_application(request):
    """Send a finished onboarding to the review queue."""
    user = request.user
    if not user.needs_approval:
        raise PermissionDenied("Only delivery leads and business developers apply.")
    if user.approval_status == User.ApprovalStatus.APPROVED:
        # Being approved before you submit is a legitimate path — an admin can
        # do it directly. Treat the submit as "finish onboarding" rather than an
        # error, so nobody is told off for completing a form they were asked to
        # complete.
        if not user.onboarding_completed_at:
            user.onboarding_completed_at = timezone.now()
            user.save(update_fields=["onboarding_completed_at"])
        return Response(UserSerializer(user).data)

    profile = ProfessionalProfile.objects.filter(user=user).first()
    missing = []
    if not user.full_name.strip():
        missing.append("your name")
    if not (profile and profile.country.strip()):
        missing.append("your country")
    if not (profile and profile.past_delivery.strip()):
        missing.append("what you've delivered before")
    if user.role == User.Role.DELIVERY_LEAD and not user.product_lines.exists():
        missing.append("at least one product line")
    if missing:
        return Response(
            {"detail": "Before submitting, add " + _readable_list(missing) + ".",
             "missing": missing},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user.submit_application()
    send_application_received(user)
    notify_admins_of_application(user)
    return Response(UserSerializer(user).data)


def _readable_list(items):
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def applications(request):
    """The review queue — pending partner applications."""
    if not request.user.is_superuser and request.user.role != User.Role.DELIVERY_LEAD:
        raise PermissionDenied("Only an admin can review applications.")
    qs = (User.objects
          .filter(approval_status=User.ApprovalStatus.PENDING)
          .select_related("professional_profile")
          .prefetch_related("product_lines")
          .order_by("applied_at"))
    return Response([
        {
            **UserSerializer(user).data,
            "profile": (ProfessionalProfileSerializer(user.professional_profile).data
                        if hasattr(user, "professional_profile") else None),
            "applied_at": user.applied_at,
        }
        for user in qs
    ])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decide_application(request, user_id):
    """Approve or reject a partner application."""
    if not request.user.is_superuser:
        raise PermissionDenied("Only an admin can approve applications.")
    target = User.objects.filter(id=user_id).first()
    if not target or not target.needs_approval:
        return Response({"detail": "No such application."},
                        status=status.HTTP_404_NOT_FOUND)

    serializer = ApprovalDecisionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if serializer.validated_data["decision"] == "approve":
        target.approve(by=request.user)
        if target.role == User.Role.BUSINESS_DEV:
            target.ensure_referral_code()
            send_business_dev_welcome(target)
        else:
            send_delivery_lead_welcome(target)
    else:
        reason = serializer.validated_data.get("reason", "")
        target.reject(reason=reason, by=request.user)
        send_application_rejected(target, reason)
    return Response(UserSerializer(target).data)


# ---------------------------------------------------------------------------
# Expert invitations — how a delivery lead builds their roster
# ---------------------------------------------------------------------------

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def invitations(request):
    """List the caller's invitations, or send a batch of them.

    POST accepts either one object or a list, because building a team is
    naturally a bulk action — a lead shouldn't fill the same form six times.
    A lead still in review may invite: their team is part of what's reviewed.
    """
    _require_lead(request.user, approved=False)

    if request.method == "POST":
        payload = request.data if isinstance(request.data, list) else [request.data]
        serializer = InvitationCreateSerializer(data=payload, many=True)
        serializer.is_valid(raise_exception=True)

        created, skipped = [], []
        for entry in serializer.validated_data:
            email = entry["email"]
            if Invitation.objects.filter(
                email__iexact=email, status=Invitation.Status.PENDING
            ).exists():
                skipped.append(email)
                continue
            invitation = Invitation.objects.create(
                email=email,
                full_name=entry.get("full_name", ""),
                specialty=entry.get("specialty", ""),
                skills=entry.get("skills", []),
                invited_by=request.user,
            )
            slugs = entry.get("product_lines") or []
            lines = (ProductLine.objects.filter(slug__in=slugs, is_active=True)
                     if slugs else request.user.product_lines.all())
            invitation.product_lines.set(lines)
            send_expert_invitation(invitation)
            created.append(invitation)

        return Response(
            {"created": InvitationSerializer(created, many=True).data,
             "skipped": skipped},
            status=status.HTTP_201_CREATED,
        )

    qs = (request.user.sent_invitations
          .prefetch_related("product_lines")
          .exclude(status=Invitation.Status.REVOKED))
    return Response(InvitationSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def revoke_invitation(request, invitation_id):
    _require_lead(request.user, approved=False)
    invitation = Invitation.objects.filter(
        id=invitation_id, invited_by=request.user
    ).first()
    if not invitation:
        return Response({"detail": "Invitation not found."},
                        status=status.HTTP_404_NOT_FOUND)
    if invitation.status == Invitation.Status.ACCEPTED:
        return Response({"detail": "That invitation was already accepted."},
                        status=status.HTTP_400_BAD_REQUEST)
    invitation.status = Invitation.Status.REVOKED
    invitation.save(update_fields=["status"])
    return Response({"detail": "Invitation revoked."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resend_invitation(request, invitation_id):
    _require_lead(request.user, approved=False)
    invitation = Invitation.objects.filter(
        id=invitation_id, invited_by=request.user,
        status=Invitation.Status.PENDING,
    ).first()
    if not invitation:
        return Response({"detail": "No pending invitation to resend."},
                        status=status.HTTP_404_NOT_FOUND)
    # Resending renews the clock, otherwise a resent-but-expired link is a
    # dead end that looks live.
    invitation.expires_at = timezone.now() + Invitation.TTL
    invitation.save(update_fields=["expires_at"])
    send_expert_invitation(invitation)
    return Response(InvitationSerializer(invitation).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def invitation_detail(request, token):
    """Public preview of an invitation, for the accept screen."""
    invitation = (Invitation.objects
                  .filter(token=token)
                  .select_related("invited_by")
                  .prefetch_related("product_lines")
                  .first())
    if not invitation:
        return Response({"detail": "This invitation link isn't valid."},
                        status=status.HTTP_404_NOT_FOUND)
    if not invitation.is_open:
        reason = ("This invitation has already been used."
                  if invitation.status == Invitation.Status.ACCEPTED
                  else "This invitation has expired or been withdrawn.")
        return Response({"detail": reason}, status=status.HTTP_410_GONE)
    return Response(InvitationSerializer(invitation).data)


@api_view(["POST"])
@permission_classes([AllowAny])
def accept_invitation(request, token):
    """The invitee creates their account with a password they choose."""
    invitation = (Invitation.objects
                  .filter(token=token)
                  .select_related("invited_by")
                  .first())
    if not invitation:
        return Response({"detail": "This invitation link isn't valid."},
                        status=status.HTTP_404_NOT_FOUND)
    if not invitation.is_open:
        return Response({"detail": "This invitation is no longer valid."},
                        status=status.HTTP_410_GONE)
    if User.objects.filter(email__iexact=invitation.email).exists():
        return Response({"detail": "An account with this email already exists."},
                        status=status.HTTP_400_BAD_REQUEST)

    serializer = InvitationAcceptSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    user = User.objects.create_user(
        email=invitation.email,
        password=data["password"],
        full_name=data.get("full_name") or invitation.full_name,
        specialty=data.get("specialty") or invitation.specialty,
        skills=invitation.skills,
        role=User.Role.EXPERT,
        lead=invitation.invited_by,
        # Accepting the invitation proves they own the address.
        is_email_verified=True,
    )
    user.product_lines.set(invitation.product_lines.all())

    invitation.status = Invitation.Status.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["status", "accepted_at"])

    notify_lead_invitation_accepted(invitation, user)
    return Response(
        {**tokens_for(user), "user": UserSerializer(user).data,
         "detail": "Welcome aboard — your account is ready."},
        status=status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# Profile: professional detail, documents, and identity verification
# ---------------------------------------------------------------------------

def _may_read_professional(viewer, owner):
    """A person's own profile, their delivery lead's view of it, or an admin's.

    A lead needs this to staff a brief — it's the CV and the skills. It is
    deliberately narrower than "any lead": only the lead this expert actually
    works under.
    """
    return (
        viewer.id == owner.id
        or viewer.is_superuser
        or (viewer.role == User.Role.DELIVERY_LEAD and owner.lead_id == viewer.id)
    )


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def my_professional_profile(request):
    """Read or update the signed-in user's professional profile."""
    profile, _ = ProfessionalProfile.objects.get_or_create(user=request.user)
    if request.method == "PATCH":
        serializer = ProfessionalProfileDetailSerializer(
            profile, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(ProfessionalProfileDetailSerializer(profile).data)


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def my_cv(request):
    """Upload or remove the signed-in user's CV."""
    profile, _ = ProfessionalProfile.objects.get_or_create(user=request.user)

    if request.method == "DELETE":
        if profile.cv:
            profile.cv.delete(save=False)
        profile.cv = None
        profile.cv_uploaded_at = None
        profile.save(update_fields=["cv", "cv_uploaded_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    upload = request.FILES.get("cv")
    if not upload:
        return Response({"detail": "Choose a file to upload."},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_cv(upload)
    except DjangoValidationError as exc:
        return Response({"detail": " ".join(exc.messages)},
                        status=status.HTTP_400_BAD_REQUEST)

    # Replacing a CV removes the old file rather than orphaning it on disk.
    if profile.cv:
        profile.cv.delete(save=False)
    profile.cv = upload
    profile.cv_uploaded_at = timezone.now()
    profile.save(update_fields=["cv", "cv_uploaded_at"])
    return Response(ProfessionalProfileDetailSerializer(profile).data,
                    status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def my_kyc(request):
    """Read or update the signed-in user's identity record.

    Editing a record that's already verified would let someone swap their
    identity after being cleared, so verified records are frozen — a genuine
    change (a new passport, a house move) goes through an admin.
    """
    if not request.user.can_earn and request.user.role != User.Role.EXPERT:
        raise PermissionDenied("Identity verification is for people the platform pays.")

    kyc, _ = KycProfile.objects.get_or_create(user=request.user)
    if request.method == "PATCH":
        if kyc.is_verified:
            return Response(
                {"detail": "Your identity is already verified. Contact support if "
                           "your details have changed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = KycSerializer(kyc, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        kyc.refresh_from_db()
    return Response(KycSerializer(kyc).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def my_id_document(request):
    """Upload the signed-in user's identity document."""
    kyc, _ = KycProfile.objects.get_or_create(user=request.user)
    if kyc.is_verified:
        return Response({"detail": "Your identity is already verified."},
                        status=status.HTTP_400_BAD_REQUEST)

    upload = request.FILES.get("document")
    if not upload:
        return Response({"detail": "Choose a file to upload."},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_id_document(upload)
    except DjangoValidationError as exc:
        return Response({"detail": " ".join(exc.messages)},
                        status=status.HTTP_400_BAD_REQUEST)

    if kyc.id_document:
        kyc.id_document.delete(save=False)
    kyc.id_document = upload
    kyc.save(update_fields=["id_document"])
    return Response(KycSerializer(kyc).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_kyc(request):
    """Send an identity record for review."""
    kyc, _ = KycProfile.objects.get_or_create(user=request.user)
    if kyc.is_verified:
        return Response({"detail": "Already verified."},
                        status=status.HTTP_400_BAD_REQUEST)
    if not kyc.is_complete:
        return Response(
            {"detail": "Some details are still missing.",
             "missing": kyc.missing_fields},
            status=status.HTTP_400_BAD_REQUEST,
        )
    kyc.submit()
    notify_admins_of_kyc(request.user)
    return Response(KycSerializer(kyc).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def kyc_queue(request):
    """Identity records awaiting review. Admins only — never a delivery lead."""
    if not request.user.is_superuser:
        raise PermissionDenied("Only an admin can review identity documents.")
    qs = (KycProfile.objects
          .filter(status=KycProfile.Status.PENDING)
          .select_related("user")
          .order_by("submitted_at"))
    return Response(KycReviewSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decide_kyc(request, user_id):
    if not request.user.is_superuser:
        raise PermissionDenied("Only an admin can review identity documents.")
    kyc = KycProfile.objects.filter(user_id=user_id).select_related("user").first()
    if not kyc:
        return Response({"detail": "No identity record found."},
                        status=status.HTTP_404_NOT_FOUND)

    serializer = KycDecisionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if serializer.validated_data["decision"] == "verify":
        kyc.verify(by=request.user)
        send_kyc_verified(kyc.user)
    else:
        reason = serializer.validated_data.get("reason", "")
        kyc.reject(reason=reason, by=request.user)
        send_kyc_rejected(kyc.user, reason)
    return Response(KycReviewSerializer(kyc).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_document(request, kind, user_id):
    """Stream a CV or an identity document to someone entitled to see it.

    These files are never served by the web server. A CV is a person's work
    history and an ID document is their passport — a guessable static URL would
    put both a link away from anyone on the internet. Everything goes through
    this check instead.

    Who may read what:
      * **CV** — the owner, their delivery lead, or an admin.
      * **ID document** — the owner or an admin. Never a delivery lead: staffing
        a project needs a CV, not a date of birth.
    """
    owner = User.objects.filter(id=user_id).first()
    if not owner:
        raise Http404

    if kind == "cv":
        profile = getattr(owner, "professional_profile", None)
        handle = profile.cv if profile else None
        allowed = _may_read_professional(request.user, owner)
        download_name = f"{(owner.full_name or owner.email).replace(' ', '-')}-CV"
    elif kind == "id":
        kyc = getattr(owner, "kyc", None)
        handle = kyc.id_document if kyc else None
        allowed = request.user.id == owner.id or request.user.is_superuser
        download_name = f"{(owner.full_name or owner.email).replace(' ', '-')}-ID"
    else:
        raise Http404

    if not allowed:
        raise PermissionDenied("You can't view this document.")
    if not handle:
        raise Http404

    # 404 rather than 500 when the row points at a file that's gone — a missing
    # file is "not found", and the difference matters when storage is remote.
    try:
        stream = handle.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404

    suffix = Path(handle.name).suffix
    response = FileResponse(stream, as_attachment=True,
                            filename=f"{download_name}{suffix}")
    # These are personal documents: never let a proxy or the browser keep a copy.
    response["Cache-Control"] = "no-store, private"
    return response
