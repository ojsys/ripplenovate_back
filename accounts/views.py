import uuid

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import send_password_reset_email, send_verification_email
from .models import EmailToken, SiteSettings
from .serializers import (
    ChangePasswordSerializer,
    DeveloperCreateSerializer,
    DeveloperUpdateSerializer,
    PasswordResetConfirmSerializer,
    ProfileUpdateSerializer,
    RegisterSerializer,
    RoleUpdateSerializer,
    UserSerializer,
)

User = get_user_model()


def _require_lead(user):
    if user.role != User.Role.DELIVERY_LEAD and not user.is_superuser:
        raise PermissionDenied("Only a delivery lead can do that.")


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
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])
    token.mark_used()
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
def developers(request):
    """List developer accounts, or (delivery lead) create a new one with a profile."""
    if request.method == "POST":
        _require_lead(request.user)
        serializer = DeveloperCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)
    qs = User.objects.filter(role=User.Role.DEVELOPER).order_by("full_name")
    return Response(UserSerializer(qs, many=True).data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_developer(request, user_id):
    """Delivery lead edits a developer's profile (name, specialty, load)."""
    _require_lead(request.user)
    target = User.objects.filter(id=user_id, role=User.Role.DEVELOPER).first()
    if not target:
        return Response({"detail": "Developer not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = DeveloperUpdateSerializer(target, data=request.data, partial=True)
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
    target.role = serializer.validated_data["role"]
    if "specialty" in serializer.validated_data:
        target.specialty = serializer.validated_data["specialty"]
    target.save(update_fields=["role", "specialty"])
    return Response(UserSerializer(target).data)
