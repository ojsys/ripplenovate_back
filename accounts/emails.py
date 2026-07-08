from django.conf import settings

from ripple.mailer import send_brand_email

from .models import EmailToken


def _link(path, token=None):
    base = settings.FRONTEND_URL.rstrip("/")
    link = f"{base}{path}" + (f"?token={token}" if token else "")
    # Console email backends encode the body as quoted-printable, which mangles
    # the link (?token=3D...). In dev, also print a clean, copyable line.
    if "console" in settings.EMAIL_BACKEND:
        print(f"\n🔗 [Ripple dev] {path} link: {link}\n", flush=True)
    return link


def _first_name(user):
    return (user.full_name or "").split(" ")[0] or "there"


def send_verification_email(user):
    token = EmailToken.objects.create(user=user, purpose=EmailToken.Purpose.VERIFY)
    link = _link("/verify-email", token.token)
    send_brand_email(
        subject="Confirm your email",
        to=user.email,
        heading="Confirm your email address",
        paragraphs=[
            f"Hi {_first_name(user)},",
            "Welcome aboard! Confirm your email to activate your account and get started.",
            "This link expires in 24 hours.",
        ],
        cta=("Verify my email", link),
    )
    return token


def send_password_reset_email(user):
    token = EmailToken.objects.create(user=user, purpose=EmailToken.Purpose.RESET)
    link = _link("/reset-password", token.token)
    send_brand_email(
        subject="Reset your password",
        to=user.email,
        heading="Reset your password",
        paragraphs=[
            f"Hi {_first_name(user)},",
            "We received a request to reset your password. Use the button below to choose a new one.",
            "This link expires in 24 hours. If you didn't request this, you can safely ignore this email.",
        ],
        cta=("Reset password", link),
    )
    return token


def send_welcome_client(user):
    """Sent once a client's email is verified."""
    send_brand_email(
        subject="Welcome to Ripple Innovation Labs",
        to=user.email,
        heading="You're all set 🎉",
        paragraphs=[
            f"Hi {_first_name(user)}, welcome to Ripple Innovation Labs.",
            "Post a project brief, get a fixed quote, pay securely with Paystack, and follow your "
            "build from brief to delivery — all in one place.",
            "Ready when you are — post your first project and our delivery lead will send a quote within a day.",
        ],
        cta=("Post a project", _link("/new")),
    )


def send_developer_welcome(user):
    """Sent when a delivery lead creates a developer account."""
    token = EmailToken.objects.create(user=user, purpose=EmailToken.Purpose.RESET)
    link = _link("/reset-password", token.token)
    send_brand_email(
        subject="Your developer account is ready",
        to=user.email,
        heading="Welcome to the Ripple talent team",
        paragraphs=[
            f"Hi {_first_name(user)},",
            f"A developer account has been created for you. You can sign in with your email ({user.email}).",
            "Set your own password using the button below, then head to your task board to see the "
            "projects assigned to you and post progress updates as you build.",
        ],
        cta=("Set your password", link),
    )
    return token


def send_delivery_lead_welcome(user):
    """Sent when a user is granted the delivery-lead role."""
    send_brand_email(
        subject="You're now a Ripple delivery lead",
        to=user.email,
        heading="Welcome, delivery lead",
        paragraphs=[
            f"Hi {_first_name(user)},",
            "Your account now has delivery-lead access. You can quote new briefs, assign developers, "
            "manage the team, and track every project from the delivery board.",
        ],
        cta=("Open the delivery board", _link("/board")),
    )
