"""Approve everyone who was already operating before approvals existed.

`approval_status` defaults to "n/a", and `is_approved` only clears a delivery
lead or business developer when their status is "approved". Without this
migration every existing lead would be unable to quote a brief or be paid the
moment this ships — locked out of a platform they were already running.

Applies only to the roles that need approval; clients and experts are never
gated, so their "n/a" is correct as it stands.
"""
from django.db import migrations
from django.utils import timezone


def approve_existing(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(
        role__in=["delivery_lead", "business_dev"],
        approval_status="n/a",
    ).update(
        approval_status="approved",
        approved_at=timezone.now(),
        # Onboarding didn't exist for them; mark it done so they aren't sent
        # back through a wizard to re-describe work they've already delivered.
        onboarding_completed_at=timezone.now(),
    )


def unapprove(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(
        role__in=["delivery_lead", "business_dev"], approval_status="approved"
    ).update(approval_status="n/a", approved_at=None, onboarding_completed_at=None)


class Migration(migrations.Migration):

    dependencies = [("accounts", "0010_onboarding_and_invitations")]

    operations = [migrations.RunPython(approve_existing, unapprove)]
