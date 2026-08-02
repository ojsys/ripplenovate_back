"""Recover a completion date for projects delivered before it was recorded.

`completed_at` is new, so every project already marked Completed has a null one
and would report no cycle time — which would make the first reporting screen
look broken rather than empty.

Best available evidence, in order:
  1. the activity entry written when delivery was approved;
  2. the earnings credited on approval;
  3. the last thing that happened on the project at all.

All three are approximations of a moment nobody recorded at the time, and the
migration is explicit about that rather than inventing a precise-looking value.
Projects with no evidence are left null, and reporting skips them.
"""
from django.db import migrations

APPROVAL_PHRASES = ["Approved delivery", "Marked the project complete",
                    "marked complete", "Project complete"]


def backfill(apps, schema_editor):
    Project = apps.get_model("projects", "Project")

    for project in Project.objects.filter(stage="Completed", completed_at__isnull=True):
        stamp = None

        # 1. The activity entry that recorded the approval.
        for activity in project.activity.order_by("-created_at"):
            if any(phrase.lower() in activity.text.lower() for phrase in APPROVAL_PHRASES):
                stamp = activity.created_at
                break

        # 2. Earnings are only written on approval, so they date it well.
        if stamp is None:
            earning = project.earnings.order_by("created_at").first()
            if earning:
                stamp = earning.created_at

        # 3. Anything at all, so the project isn't invisible to reporting.
        if stamp is None:
            last = project.activity.order_by("-created_at").first()
            stamp = last.created_at if last else None

        if stamp:
            project.completed_at = stamp
            project.save(update_fields=["completed_at"])


def clear(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Project.objects.update(completed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0012_project_completed_at"),
        ("payments", "0006_business_developer"),
    ]

    operations = [migrations.RunPython(backfill, clear)]
