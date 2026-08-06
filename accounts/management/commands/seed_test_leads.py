"""Create plain delivery-lead accounts for testing project scoping.

The only lead in a fresh dev database is the superuser, and `is_superuser`
short-circuits every access check — so testing "what can a delivery lead see?"
with that account always answers "everything", no matter what the scoping rules
say. These are ordinary leads: no staff flag, no superuser flag, each running a
single discipline.

    python manage.py seed_test_leads

Idempotent — re-run it after resetting the database. Refuses to run with
DEBUG off, because it sets known passwords.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import ProductLine

User = get_user_model()

PASSWORD = "ripple-dev-1234"

# One lead per discipline, so "can Sam see Dami's work?" is a question the data
# can actually answer. Plus one still in review, to exercise the approval gate.
LEADS = [
    {
        "email": "sam.web@ril.team",
        "full_name": "Sam Okafor",
        "line": "software-web",
        "approved": True,
    },
    {
        "email": "dami.design@ril.team",
        "full_name": "Dami Adeyemi",
        "line": "design-creative",
        "approved": True,
    },
    {
        "email": "chidi.web@ril.team",
        "full_name": "Chidi Nwosu",
        "line": "software-web",
        "approved": True,
    },
    {
        "email": "tolu.pending@ril.team",
        "full_name": "Tolu Bakare",
        "line": "software-web",
        "approved": False,
    },
]


class Command(BaseCommand):
    help = "Create non-superuser delivery leads for testing project scoping."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to run with DEBUG off — this sets known passwords."
            )

        for spec in LEADS:
            line = ProductLine.objects.filter(slug=spec["line"]).first()
            if not line:
                raise CommandError(f"No product line {spec['line']!r}.")

            user, created = User.objects.get_or_create(
                email=spec["email"],
                defaults={
                    "full_name": spec["full_name"],
                    "role": User.Role.DELIVERY_LEAD,
                },
            )
            # Reset every time: an account left half-way through the signup
            # wizard lands on /onboarding instead of the board, which reads as
            # "the board is broken".
            user.set_password(PASSWORD)
            user.is_email_verified = True
            user.is_staff = False
            user.is_superuser = False
            user.save()
            user.product_lines.set([line])

            if spec["approved"]:
                user.approve()
            else:
                user.submit_application()

            state = "created" if created else "reset"
            status = "approved" if spec["approved"] else "PENDING review"
            self.stdout.write(
                f"  {state:7} {user.email:24} {spec['line']:16} {status}"
            )

        self.stdout.write(self.style.SUCCESS(f"\nPassword for all: {PASSWORD}"))
        self.stdout.write(
            "These are ordinary leads — no superuser flag — so they exercise the "
            "real scoping rules."
        )
