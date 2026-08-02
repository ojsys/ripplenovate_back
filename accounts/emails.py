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


def send_expert_welcome(user):
    """Sent when an expert account is created directly (admin role change).

    The normal path is an invitation — see send_expert_invitation.
    """
    token = EmailToken.objects.create(user=user, purpose=EmailToken.Purpose.RESET)
    link = _link("/reset-password", token.token)
    send_brand_email(
        subject="Your expert account is ready",
        to=user.email,
        heading="Welcome to the Ripple talent team",
        paragraphs=[
            f"Hi {_first_name(user)},",
            f"An expert account has been created for you. You can sign in with your email ({user.email}).",
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
            "Your account now has delivery-lead access. You can quote new briefs, assign experts, "
            "manage the team, and track every project from the delivery board.",
        ],
        cta=("Open the delivery board", _link("/board")),
    )


def send_business_dev_welcome(user):
    """Sent when a user is granted the business-developer role."""
    code = user.ensure_referral_code()
    base = settings.FRONTEND_URL.rstrip("/")
    send_brand_email(
        subject="You're now a Ripple business developer",
        to=user.email,
        heading="Welcome, business developer",
        paragraphs=[
            f"Hi {_first_name(user)},",
            "You can now bring clients onto the platform and earn a commission on "
            "every project they complete.",
            f"Your referral link is {base}/register?ref={code} — anyone who signs up "
            "through it is credited to you, and so is every project they post.",
            "Your commission is credited when the client approves delivery, and you "
            "can withdraw it to your bank account from the Earnings page.",
        ],
        cta=("Open your pipeline", _link("/pipeline")),
    )


def send_expert_invitation(invitation):
    """Invite someone onto a delivery lead's roster.

    The link carries a token, not a password: the invitee sets their own, so a
    lead never handles a credential belonging to someone else.
    """
    base = settings.FRONTEND_URL.rstrip("/")
    link = f"{base}/join?token={invitation.token}"
    if "console" in settings.EMAIL_BACKEND:
        print(f"\n🔗 [Ripple dev] invitation link: {link}\n", flush=True)
    inviter = invitation.invited_by.full_name or invitation.invited_by.email
    lines = ", ".join(line.name for line in invitation.product_lines.all())
    paragraphs = [
        f"Hi {(invitation.full_name or '').split(' ')[0] or 'there'},",
        f"{inviter} has invited you to join Ripple Innovation Labs as a project "
        "delivery expert. You'd work on client projects they scope and assign, "
        "and earn a share of every project you deliver.",
    ]
    if lines:
        paragraphs.append(f"You've been invited into: {lines}.")
    paragraphs.append(
        "Accept below to set your password and see your work board. This "
        "invitation expires in 14 days."
    )
    send_brand_email(
        subject=f"{inviter} invited you to Ripple Innovation Labs",
        to=invitation.email,
        heading="You've been invited to deliver with Ripple",
        paragraphs=paragraphs,
        cta=("Accept the invitation", link),
    )


def notify_lead_invitation_accepted(invitation, user):
    """Tell the lead their invitee is on board."""
    send_brand_email(
        subject=f"{user.full_name or user.email} joined your team",
        to=invitation.invited_by.email,
        heading="Your invitation was accepted",
        paragraphs=[
            f"Hi {_first_name(invitation.invited_by)},",
            f"{user.full_name or user.email} has accepted your invitation and is now "
            "on your team. You can assign them work in the product lines they cover.",
        ],
        cta=("Open my team", _link("/team")),
    )


def send_application_received(user):
    """Confirm a submitted partner application."""
    role = "delivery lead" if user.role == user.Role.DELIVERY_LEAD else "business developer"
    send_brand_email(
        subject="We've received your application",
        to=user.email,
        heading="Application received",
        paragraphs=[
            f"Hi {_first_name(user)},",
            f"Thanks for applying to join Ripple Innovation Labs as a {role}. "
            "Our team reviews applications within a couple of working days, and "
            "we'll email you as soon as there's a decision.",
            "In the meantime you can keep signing in — you'll see your dashboard, "
            "and everything unlocks the moment you're approved.",
        ],
    )


def notify_admins_of_application(user):
    """Put a new application in front of whoever reviews them."""
    from .models import User as UserModel

    admins = list(
        UserModel.objects.filter(is_superuser=True, is_active=True)
        .values_list("email", flat=True)
    )
    if not admins:
        return
    role = "Delivery lead" if user.role == user.Role.DELIVERY_LEAD else "Business developer"
    send_brand_email(
        subject=f"New {role.lower()} application: {user.full_name or user.email}",
        to=set(admins),
        heading="A new application needs review",
        paragraphs=[
            f"{role} application from {user.full_name or user.email} ({user.email}).",
            "Review it in the console and approve or decline.",
        ],
        cta=("Review applications", _link("/applications")),
    )


def send_application_rejected(user, reason=""):
    """Tell an applicant the answer is no — with the reason, if there is one."""
    paragraphs = [
        f"Hi {_first_name(user)},",
        "Thanks for your interest in joining Ripple Innovation Labs. After reviewing "
        "your application, we're not able to move forward with it right now.",
    ]
    if reason:
        paragraphs.append(f"Feedback from the team: {reason}")
    paragraphs.append(
        "You're welcome to apply again in future — reply to this email if you'd "
        "like to talk it through."
    )
    send_brand_email(
        subject="An update on your application",
        to=user.email,
        heading="About your application",
        paragraphs=paragraphs,
    )
