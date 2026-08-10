"""Drop `Task.done` now that 0019 has translated it into `status`.

`done` lives on as a property on the model, so everything reading it — the
board, `progress_pct`, the mobile task list — carries on unchanged.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0019_backfill_experts_and_task_status'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='task',
            name='done',
        ),
    ]
