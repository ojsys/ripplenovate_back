import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
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
    notify_added_to_organisation,
    notify_lead_offboarded,
    notify_roster_changed,
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
from . import impersonation
from .models import (
    EmailToken,
    ImpersonationEvent,
    Notification,
    Organisation,
    OrganisationMember,
    TermsAcceptance,
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


def _require_admin(user):
    """Gate an action to a superuser.

    `is_staff` is deliberately not enough. Staff see the platform's books;
    signing in as another person is a different order of power.
    """
    if not user.is_superuser:
        raise PermissionDenied("Only an administrator can do that.")


def me_payload(request):
    """The signed-in user, plus who is really driving if it isn't them.

    The frontend needs `impersonated_by` on every read of `/auth/me`, not just
    on the response that starts the session — a page reload must still put the
    banner back, or an admin can forget whose account they're in.
    """
    data = UserSerializer(request.user, context={"viewer": request.user}).data
    admin_id = impersonation.impersonator_id(request)
    admin = User.objects.filter(id=admin_id).first() if admin_id else None
    data["impersonated_by"] = (
        {"id": admin.id, "email": admin.email, "full_name": admin.full_name}
        if admin else None
    )
    # Folded in here rather than given its own poll: /auth/me already runs on
    # every page load, and a second request per page to render a badge is a
    # cost the badge doesn't justify.
    data["unread_notifications"] = Notification.objects.filter(
        user=request.user, read_at__isnull=True).count()
    return data


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
    if user.role == User.Role.CLIENT:
        # Every client buys through an organisation, including the sole trader
        # who has never heard the word — one code path downstream.
        Organisation.ensure_for(user)
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
    return Response(me_payload(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Change the signed-in user's password (requires the current one)."""
    # Knowing the old password is the check that this is really them — an
    # admin borrowing the account doesn't know it, and shouldn't be able to set
    # a new one and lock the owner out.
    impersonation.forbid_while_impersonating(request, "Changing a password")
    serializer = ChangePasswordSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not request.user.check_password(serializer.validated_data["old_password"]):
        return Response({"detail": "Your current password is incorrect."},
                        status=status.HTTP_400_BAD_REQUEST)
    request.user.set_password(serializer.validated_data["new_password"])
    request.user.save(update_fields=["password"])
    return Response({"detail": "Password updated."})


def _client_ip(request):
    """Best effort. Behind a proxy the left-most XFF entry is the caller."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_directory(request):
    """Everyone on the platform, for an admin to search. `?q=` by name or email.

    Deliberately admin-only and deliberately not paginated by role: the point
    is to find one specific person whose account you need to stand in, and you
    usually only know a fragment of their email.
    """
    # Admins see everyone — that's the impersonation directory. A delivery
    # lead is narrowed to clients and nothing else: they need one to set up a
    # retainer for, and that is not a reason to hand them the whole user list
    # including other leads' experts.
    is_admin = request.user.is_superuser
    if not is_admin:
        if request.user.role != User.Role.DELIVERY_LEAD or not request.user.is_approved:
            raise PermissionDenied("Only an administrator can do that.")
    qs = User.objects.all() if is_admin else User.objects.filter(
        role=User.Role.CLIENT)
    term = (request.query_params.get("q") or "").strip()
    if term:
        qs = qs.filter(Q(email__icontains=term) | Q(full_name__icontains=term))
    role = request.query_params.get("role")
    if role and is_admin:
        qs = qs.filter(role=role)
    qs = qs.prefetch_related("product_lines").order_by("full_name", "email")[:50]
    return Response(
        UserSerializer(qs, many=True, context={"viewer": request.user}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def impersonate(request, user_id):
    """Sign in as another user. Superusers only.

    Returns a token pair for the target that still remembers who opened it, so
    `/impersonation/stop` can hand the admin back their own session without
    anyone having to stash the original tokens client-side — a copy of an
    admin's credentials sitting in localStorage would be a worse problem than
    the one this solves.
    """
    _require_admin(request.user)
    target = User.objects.filter(id=user_id).first()
    if not target:
        return Response({"detail": "No user with that id."},
                        status=status.HTTP_404_NOT_FOUND)
    if target.id == request.user.id:
        return Response({"detail": "You're already signed in as yourself."},
                        status=status.HTTP_400_BAD_REQUEST)
    # Nesting would make "who is really doing this?" a chain to unwind, and the
    # `imp` claim only has room for one answer. Step out first.
    if impersonation.is_impersonated(request):
        return Response(
            {"detail": "Stop impersonating before signing in as somebody else."},
            status=status.HTTP_400_BAD_REQUEST)

    # Written before the token exists, so there is no window in which a session
    # is live but unlogged.
    event = ImpersonationEvent.objects.create(
        impersonator=request.user,
        target=target,
        reason=(request.data.get("reason") or "").strip()[:200],
        ip_address=_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
    )
    tokens = impersonation.tokens_for_impersonation(request.user, target, event.id)
    data = UserSerializer(target, context={"viewer": target}).data
    data["impersonated_by"] = {
        "id": request.user.id,
        "email": request.user.email,
        "full_name": request.user.full_name,
    }
    return Response({**tokens, "user": data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stop_impersonating(request):
    """Hand the session back and become yourself again.

    The admin's identity comes from the token's own claim, not from anything
    the caller sends — so this can only ever return you to the account that
    started the session.
    """
    admin_id = impersonation.impersonator_id(request)
    if not admin_id:
        return Response({"detail": "You aren't impersonating anyone."},
                        status=status.HTTP_400_BAD_REQUEST)
    admin = User.objects.filter(id=admin_id, is_superuser=True).first()
    if not admin:
        # Their admin rights were removed mid-session. There is nothing safe to
        # return to, so the session simply ends.
        return Response(
            {"detail": "That admin account is no longer available. Please sign in again."},
            status=status.HTTP_403_FORBIDDEN)

    event = ImpersonationEvent.objects.filter(
        id=impersonation.event_id(request), ended_at__isnull=True).first()
    if event:
        event.ended_at = timezone.now()
        event.save(update_fields=["ended_at"])
    return Response({**tokens_for(admin),
                     "user": UserSerializer(admin, context={"viewer": admin}).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def impersonation_log(request):
    """Who has been signed in as whom. Admin-only.

    An audit trail nobody can read is just a table, so this is the screen that
    makes the logging worth doing.
    """
    _require_admin(request.user)
    events = (ImpersonationEvent.objects
              .select_related("impersonator", "target")[:200])
    return Response([
        {
            "id": e.id,
            "impersonator": e.impersonator.full_name or e.impersonator.email,
            "target": e.target.full_name or e.target.email,
            "target_role": e.target.role_label,
            "reason": e.reason,
            "started_at": e.started_at,
            "ended_at": e.ended_at,
            "ip_address": e.ip_address,
        }
        for e in events
    ])


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def experts(request):
    """List expert accounts, or (delivery lead) create a new one with a profile.

    Filters:
      ``?mine=1``            only the experts on the caller's own roster
      ``?product_line=slug`` that discipline, *plus* the caller's own roster

    The assignment picker uses the second: your own team, whatever they're
    tagged with, and then everyone else who covers the brief's discipline.
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
        # Your own people are never filtered out by discipline. A product line
        # is copied onto an expert from whoever's roster they landed on — no
        # one curates it, and no lead can edit it — so it says where they came
        # from, not what they can do. Filtering your roster on it emptied the
        # "Your team" group without a word and left leads looking at other
        # people's experts, with no route back to their own. Past your roster
        # the filter still holds: those are experts you haven't vouched for,
        # and the tag is the only thing there is to go on.
        qs = qs.filter(Q(product_lines__slug=line_slug) | Q(lead=request.user))
    qs = (qs.select_related("lead").prefetch_related("product_lines")
            .distinct().order_by("full_name"))
    return Response(
        UserSerializer(qs, many=True, context={"viewer": request.user}).data)


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def roster(request, user_id):
    """Add an existing expert to your roster, or let them go.

    An expert who already has an account can't be invited again — an invitation
    creates one. Without this there was no other way to pick them up, so a lead
    who'd worked with someone before the platform changed had no route back to
    them, and re-inviting (the only obvious move) is exactly what's blocked.

    Any delivery lead may do this, deliberately. Capacity is the real limit on
    whether an expert can take work, not which lead first signed them up, and
    `active_projects` on the picker is what answers that. Where someone is
    already on another lead's roster this moves them — so that lead is told,
    rather than finding out by noticing an absence.
    """
    _require_lead(request.user)
    target = User.objects.filter(id=user_id, role=User.Role.EXPERT).first()
    if not target:
        return Response({"detail": "No expert with that id."},
                        status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        if target.lead_id != request.user.id:
            return Response({"detail": "They aren't on your roster."},
                            status=status.HTTP_400_BAD_REQUEST)
        target.lead = None
        target.save(update_fields=["lead"])
        return Response(
            UserSerializer(target, context={"viewer": request.user}).data)

    if target.lead_id == request.user.id:
        return Response({"detail": f"{target.full_name or target.email} is "
                                   "already on your team."},
                        status=status.HTTP_400_BAD_REQUEST)

    previous = target.lead
    target.lead = request.user
    target.save(update_fields=["lead"])
    # An expert can only be assigned in a discipline they cover, so a roster
    # move that leaves them coverless would be a dead end. Widen them into the
    # lines their new lead runs, keeping whatever they already had.
    target.product_lines.add(*request.user.product_lines.all())
    notify_roster_changed(target, new_lead=request.user, previous_lead=previous)
    return Response(
        UserSerializer(target, context={"viewer": request.user}).data)


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
    """The review queue — pending partner applications. Admins only.

    The check used to admit any delivery lead, which disagreed with its own
    error message, with `decide_application` below, and with what the queue
    contains: somebody else's application, including the CV and profile they
    submitted to the platform rather than to a peer. Vetting a partner is an
    admin's job, and a lead can't act on one of these anyway.
    """
    if not request.user.is_superuser:
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

    "Invite" means *get this person onto my team*. Where they already have an
    expert account there's nothing to invite them to — an invitation creates an
    account — so they simply join the roster instead. Someone who worked with
    another lead, or with this one previously, comes back the same way. The
    response says which of the three happened to each address.
    """
    _require_lead(request.user, approved=False)

    if request.method == "POST":
        payload = request.data if isinstance(request.data, list) else [request.data]
        serializer = InvitationCreateSerializer(data=payload, many=True)
        serializer.is_valid(raise_exception=True)

        created, added, skipped = [], [], []
        for entry in serializer.validated_data:
            email = entry["email"]

            # Already has an expert account: bring them onto the team rather
            # than sending an invitation they could never accept.
            existing = User.objects.filter(
                email__iexact=email, role=User.Role.EXPERT).first()
            if existing:
                if existing.lead_id == request.user.id:
                    skipped.append(
                        {"email": email, "reason": "already on your team"})
                    continue
                previous = existing.lead
                existing.lead = request.user
                existing.save(update_fields=["lead"])
                # Without a shared discipline they'd be unassignable, so they
                # pick up their new lead's lines on top of their own.
                slugs = entry.get("product_lines") or []
                lines = (ProductLine.objects.filter(slug__in=slugs, is_active=True)
                         if slugs else request.user.product_lines.all())
                existing.product_lines.add(*lines)
                notify_roster_changed(existing, new_lead=request.user,
                                      previous_lead=previous)
                added.append(existing)
                continue

            if Invitation.objects.filter(
                email__iexact=email, status=Invitation.Status.PENDING
            ).exists():
                skipped.append(
                    {"email": email, "reason": "already invited and waiting"})
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
             "added": UserSerializer(added, many=True,
                                     context={"viewer": request.user}).data,
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


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def notifications(request):
    """The signed-in user's bell.

    GET returns the most recent 50 — a bell is for what just happened, and
    anything older is answered by the project page itself. POST marks them
    read: everything, or the ids given.
    """
    if request.method == "POST":
        ids = request.data.get("ids")
        qs = Notification.objects.filter(user=request.user, read_at__isnull=True)
        if ids:
            qs = qs.filter(id__in=ids)
        qs.update(read_at=timezone.now())
        return Response({"unread": Notification.objects.filter(
            user=request.user, read_at__isnull=True).count()})

    rows = Notification.objects.filter(user=request.user)[:50]
    return Response({
        "unread": sum(1 for n in rows if n.read_at is None),
        "notifications": [
            {
                "id": n.id,
                "title": n.title,
                "body": n.body,
                "url": n.url,
                "read": n.read_at is not None,
                "created_at": n.created_at,
            }
            for n in rows
        ],
    })


# ---------------------------------------------------------------------------
# Client organisations
# ---------------------------------------------------------------------------

def _org_seat(user, organisation_id=None):
    """The caller's seat, defaulting to their only one.

    Most clients belong to exactly one organisation and should never have to
    say which. Passing an id matters only for the rare person who sits at two.
    """
    seats = list(user.organisation_memberships.select_related("organisation"))
    if not seats:
        return None
    if organisation_id:
        return next(
            (s for s in seats if s.organisation_id == int(organisation_id)), None
        )
    return seats[0]


def _org_payload(seat):
    org = seat.organisation
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "billing_email": org.billing_email,
        "preferred_currency": org.preferred_currency,
        "my_role": seat.role,
        "can_manage": seat.role == OrganisationMember.Role.OWNER,
        "members": [
            {
                "id": m.user_id,
                "name": m.user.full_name or m.user.email,
                "email": m.user.email,
                "role": m.role,
                "role_label": m.get_role_display(),
                "initials": m.user.initials,
            }
            for m in org.memberships.select_related("user")
        ],
    }


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def my_organisation(request):
    """The company the signed-in client buys through.

    PATCH is the owner's: renaming it, and setting where invoices go when
    that isn't the person who posted the brief.
    """
    seat = _org_seat(request.user, request.query_params.get("organisation"))
    if not seat:
        return Response({"detail": "You don't belong to an organisation."},
                        status=status.HTTP_404_NOT_FOUND)
    if request.method == "PATCH":
        if seat.role != OrganisationMember.Role.OWNER:
            raise PermissionDenied("Only an owner can change the company details.")
        org = seat.organisation
        name = (request.data.get("name") or "").strip()
        if name:
            org.name = name[:150]
        if "billing_email" in request.data:
            org.billing_email = (request.data.get("billing_email") or "").strip()
        if "preferred_currency" in request.data:
            # Which rail can carry their charges follows from this, so an
            # unrecognised code would leave them unable to pay at all. Blank
            # is always valid and means "the platform default".
            from payments import gateways, stripe_gateway

            code = (request.data.get("preferred_currency") or "").strip().upper()
            allowed = gateways.PAYSTACK_CURRENCIES | stripe_gateway.CURRENCIES
            if code and code not in allowed:
                return Response(
                    {"detail": f"We can't currently charge in {code}."},
                    status=status.HTTP_400_BAD_REQUEST)
            org.preferred_currency = code
        org.save(update_fields=["name", "billing_email", "preferred_currency"])
    return Response(_org_payload(seat))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def organisation_members(request):
    """Add a colleague to the company, by email.

    Deliberately only connects accounts that already exist. Creating one from
    an email address here would mean a stranger's address could be turned into
    a login by anyone who guessed it, and the invitation machinery that does
    this properly already exists for experts — this is not the place to build a
    second, weaker one.
    """
    seat = _org_seat(request.user, request.data.get("organisation"))
    if not seat:
        return Response({"detail": "You don't belong to an organisation."},
                        status=status.HTTP_404_NOT_FOUND)
    if seat.role != OrganisationMember.Role.OWNER:
        raise PermissionDenied("Only an owner can add people to the company.")

    email = (request.data.get("email") or "").strip().lower()
    role = request.data.get("role") or OrganisationMember.Role.MEMBER
    if role not in OrganisationMember.Role.values:
        return Response({"detail": "Pick a valid role."},
                        status=status.HTTP_400_BAD_REQUEST)
    person = User.objects.filter(email__iexact=email).first()
    if not person:
        return Response(
            {"detail": "No account with that email yet. Ask them to sign up "
                       "first, then add them here."},
            status=status.HTTP_400_BAD_REQUEST)
    if person.role != User.Role.CLIENT:
        return Response(
            {"detail": f"{person.full_name or email} works on the delivery side, "
                       "so they can't be added as a buyer."},
            status=status.HTTP_400_BAD_REQUEST)

    membership, created = OrganisationMember.objects.get_or_create(
        organisation=seat.organisation, user=person,
        defaults={"role": role, "invited_by": request.user},
    )
    if not created:
        return Response({"detail": "They're already on this company."},
                        status=status.HTTP_400_BAD_REQUEST)
    notify_added_to_organisation(membership)
    return Response(_org_payload(_org_seat(request.user, seat.organisation_id)),
                    status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def organisation_member(request, user_id):
    """Change a colleague's seat, or remove them."""
    seat = _org_seat(request.user, request.data.get("organisation"))
    if not seat or seat.role != OrganisationMember.Role.OWNER:
        raise PermissionDenied("Only an owner can manage the company's people.")
    target = OrganisationMember.objects.filter(
        organisation=seat.organisation, user_id=user_id).first()
    if not target:
        return Response({"detail": "They aren't on this company."},
                        status=status.HTTP_404_NOT_FOUND)

    owners = [m for m in seat.organisation.memberships.all()
              if m.role == OrganisationMember.Role.OWNER]
    losing_last_owner = (
        target.role == OrganisationMember.Role.OWNER and len(owners) == 1
    )
    if request.method == "DELETE":
        if losing_last_owner:
            # Otherwise the company is left with projects nobody can administer.
            return Response(
                {"detail": "Make somebody else an owner first — a company can't "
                           "be left without one."},
                status=status.HTTP_400_BAD_REQUEST)
        removing_self = target.user_id == request.user.id
        target.delete()
        if removing_self:
            # They've just given up the seat this response would be built from.
            # Say so plainly rather than 500-ing on a membership that no
            # longer exists.
            return Response({"detail": "You've left this company."})
    else:
        role = request.data.get("role")
        if role not in OrganisationMember.Role.values:
            return Response({"detail": "Pick a valid role."},
                            status=status.HTTP_400_BAD_REQUEST)
        if losing_last_owner and role != OrganisationMember.Role.OWNER:
            return Response(
                {"detail": "Make somebody else an owner first — a company can't "
                           "be left without one."},
                status=status.HTTP_400_BAD_REQUEST)
        target.role = role
        target.save(update_fields=["role"])
    return Response(_org_payload(_org_seat(request.user, seat.organisation_id)))


# ---------------------------------------------------------------------------
# Terms, and letting a delivery lead go
# ---------------------------------------------------------------------------

def _terms_state(user):
    version = settings.TERMS_VERSION
    return {
        "version": version,
        "accepted": TermsAcceptance.objects.filter(
            user=user, version=version).exists(),
        # Only the delivery side is gated. A client agreeing to terms is a
        # signup checkbox; a partner agreeing to non-circumvention terms is the
        # thing that has to be recorded and re-asked when they change.
        "required": user.role in (User.Role.DELIVERY_LEAD, User.Role.EXPERT,
                                  User.Role.BUSINESS_DEV),
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def terms(request):
    """Read whether the signed-in user is on the current terms, or accept them."""
    if request.method == "POST":
        TermsAcceptance.objects.get_or_create(
            user=request.user, version=settings.TERMS_VERSION,
            defaults={"ip_address": _client_ip(request)},
        )
    return Response(_terms_state(request.user))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def offboard_lead(request, user_id):
    """Hand a departing delivery lead's book to somebody else.

    Until now a lead leaving left orphans: experts with a dangling `lead`, live
    projects with an owner who no longer works here, and retainers that would
    keep billing into nobody's inbox. The roster and the client relationships
    are also exactly where the disintermediation risk actually sits — not with
    the experts, who never met the client.

    Deliberately **does not touch completed earnings.** Money already credited
    belongs to the person who earned it, and reassigning a project must never
    restate who was paid for it.
    """
    _require_admin(request.user)
    leaving = User.objects.filter(
        id=user_id, role=User.Role.DELIVERY_LEAD).first()
    if not leaving:
        return Response({"detail": "No delivery lead with that id."},
                        status=status.HTTP_404_NOT_FOUND)

    successor_id = request.data.get("successor")
    successor = User.objects.filter(
        id=successor_id, role=User.Role.DELIVERY_LEAD).first() if successor_id else None
    if not successor:
        return Response(
            {"detail": "Name another delivery lead to take this on."},
            status=status.HTTP_400_BAD_REQUEST)
    if successor.id == leaving.id:
        return Response({"detail": "Pick somebody else."},
                        status=status.HTTP_400_BAD_REQUEST)
    if not successor.is_approved:
        return Response(
            {"detail": "That lead's account is still being reviewed."},
            status=status.HTTP_400_BAD_REQUEST)

    from projects.models import Engagement, Project

    # The roster. Widened into the successor's lines so nobody becomes
    # unassignable, the same rule a voluntary roster move already follows.
    roster = list(leaving.team_members.filter(role=User.Role.EXPERT))
    for expert in roster:
        expert.lead = successor
        expert.save(update_fields=["lead"])
        expert.product_lines.add(*successor.product_lines.all())

    # Live work only. A completed project keeps the lead who delivered it —
    # rewriting that would misattribute history and disagree with the ledger.
    live = Project.objects.filter(lead=leaving).exclude(
        stage__in=Project.CLOSED_STAGES)
    moved_projects = live.count()
    live.update(lead=successor)

    retainers = Engagement.objects.filter(lead=leaving).exclude(
        status=Engagement.Status.ENDED)
    moved_retainers = retainers.count()
    retainers.update(lead=successor)

    outstanding = _unsettled_balance(leaving)
    notify_lead_offboarded(leaving, successor, moved_projects, len(roster))

    return Response({
        "detail": f"{leaving.full_name or leaving.email}'s book moved to "
                  f"{successor.full_name or successor.email}.",
        "experts_moved": len(roster),
        "projects_moved": moved_projects,
        "retainers_moved": moved_retainers,
        # Reported rather than settled: paying somebody out is a decision with
        # a two-person rule on it, and it doesn't belong inside a bulk move.
        "outstanding_balance_usd": str(outstanding),
    })


def _unsettled_balance(user):
    from payments import earnings as earnings_service

    try:
        return earnings_service.available_balance(user)
    except Exception:  # noqa: BLE001 — reporting a balance must not block the move
        return Decimal("0.00")
