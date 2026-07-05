from django.conf import settings
from django.core.mail import send_mail

from .models import EmailToken


def _link(path, token):
    base = settings.FRONTEND_URL.rstrip("/")
    link = f"{base}{path}?token={token}"
    # Console email backends encode the body as quoted-printable, which mangles
    # the link (?token=3D...). In dev, also print a clean, copyable line.
    if "console" in settings.EMAIL_BACKEND:
        print(f"\n🔗 [Ripple dev] {path} link: {link}\n", flush=True)
    return link


def send_verification_email(user):
    token = EmailToken.objects.create(user=user, purpose=EmailToken.Purpose.VERIFY)
    link = _link("/verify-email", token.token)
    send_mail(
        subject="Verify your Ripple Innovation Labs account",
        message=(
            f"Hi {user.full_name or user.email},\n\n"
            "Welcome to Ripple Innovation Labs. Confirm your email to activate your account:\n\n"
            f"{link}\n\n"
            "This link expires in 24 hours.\n\n"
            "— Ripple Innovation Labs"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
    return token


def send_password_reset_email(user):
    token = EmailToken.objects.create(user=user, purpose=EmailToken.Purpose.RESET)
    link = _link("/reset-password", token.token)
    send_mail(
        subject="Reset your Ripple Innovation Labs password",
        message=(
            f"Hi {user.full_name or user.email},\n\n"
            "We received a request to reset your password. Use the link below:\n\n"
            f"{link}\n\n"
            "This link expires in 24 hours. If you didn't request this, ignore this email.\n\n"
            "— Ripple Innovation Labs"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
    return token
