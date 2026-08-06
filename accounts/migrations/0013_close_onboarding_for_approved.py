"""Close onboarding for anyone an admin already approved.

`approve()` now stamps `onboarding_completed_at`, but people approved before
that fix still have a null one — and the app reads that as "hasn't finished
signing up", which bounced them back into the wizard from every page they
opened. This releases them.
"""
from django.db import migrations
from django.utils import timezone


def close(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(
        approval_status="approved", onboarding_completed_at__isnull=True
    ).update(onboarding_completed_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("accounts", "0012_professional_profile_and_kyc")]
    operations = [migrations.RunPython(close, migrations.RunPython.noop)]
