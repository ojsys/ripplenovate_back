import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


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
        DEVELOPER = "developer", "Developer"

    # Drop the username field — email is the identifier.
    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CLIENT)

    # Client-specific
    company = models.CharField(max_length=150, blank=True)
    # Developer-specific
    specialty = models.CharField(max_length=150, blank=True)
    active_load = models.PositiveIntegerField(default=0)

    is_email_verified = models.BooleanField(default=False)

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
        if self.role == self.Role.DELIVERY_LEAD:
            return "Delivery Lead"
        return self.get_role_display()


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
