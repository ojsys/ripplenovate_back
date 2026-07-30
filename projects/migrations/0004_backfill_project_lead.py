"""Attribute existing quoted-or-later projects to a delivery lead.

Projects created before leads were tracked have no `lead`, which would leave the
delivery-lead share of their value unattributed. Best available signal: the lead
who actually posted on the project, falling back to the longest-standing lead.
"""
from django.db import migrations

SUBMITTED = "Submitted"
DELIVERY_LEAD = "delivery_lead"


def backfill_lead(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Activity = apps.get_model("projects", "Activity")
    User = apps.get_model("accounts", "User")

    fallback = (
        User.objects.filter(role=DELIVERY_LEAD).order_by("id").values_list("id", flat=True).first()
    )
    if fallback is None:
        return

    for project in Project.objects.filter(lead__isnull=True).exclude(stage=SUBMITTED):
        lead_id = (
            Activity.objects.filter(project=project, author__role=DELIVERY_LEAD)
            .order_by("created_at", "id")
            .values_list("author_id", flat=True)
            .first()
        )
        project.lead_id = lead_id or fallback
        project.save(update_fields=["lead"])


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0003_project_lead"),
        ("accounts", "0004_sitesettings_delivery_lead_share_percent_and_more"),
    ]

    # Reversing just clears the attribution again.
    operations = [
        migrations.RunPython(backfill_lead, migrations.RunPython.noop),
    ]
