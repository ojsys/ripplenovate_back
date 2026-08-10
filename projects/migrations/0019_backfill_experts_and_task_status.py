"""Move the existing data onto the new shape.

Two translations, both of which have to leave every project paying out exactly
what it would have paid out before:

* Every project's `expert` joins its `experts` team. Nothing reads the team yet,
  but from here on "who is delivering this?" has one answer.
* `done` becomes `status`. A ticked task was the only "finished" state that
  existed, so it maps to APPROVED. Legacy tasks are all worth $0.00 — no task
  had a price before this migration — so nothing here releases money.

The reverse puts `done` back from `status`. It can't restore a team that was
never a single expert, which is why the forward direction is the safe one and
the backup taken before running this is the real undo.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Task = apps.get_model("projects", "Task")

    for project in Project.objects.exclude(expert__isnull=True).iterator():
        project.experts.add(project.expert_id)

    Task.objects.filter(done=True).update(status="approved")
    Task.objects.filter(done=False).update(status="todo")


def backwards(apps, schema_editor):
    Task = apps.get_model("projects", "Task")
    Task.objects.filter(status="approved").update(done=True)
    Task.objects.exclude(status="approved").update(done=False)


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0018_multi_expert_and_task_payouts'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
