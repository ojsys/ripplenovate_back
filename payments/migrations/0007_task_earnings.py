"""Let an Earning belong to a single approved task.

The old `unique_earning_per_role` constraint becomes two. A NULL `task` can't
take part in a plain unique index — every project-level row would look distinct
from every other and the "once per role" rule would quietly stop holding — so
the project-level and task-level rules are indexed separately.

Existing rows all have `task = NULL` and stay covered by the first constraint.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0006_business_developer'),
        ('projects', '0020_drop_task_done'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='earning',
            name='unique_earning_per_role',
        ),
        migrations.AddField(
            model_name='earning',
            name='task',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='earnings', to='projects.task'),
        ),
        migrations.AddConstraint(
            model_name='earning',
            constraint=models.UniqueConstraint(condition=models.Q(('task__isnull', True)), fields=('project', 'user', 'kind'), name='unique_project_earning_per_role'),
        ),
        migrations.AddConstraint(
            model_name='earning',
            constraint=models.UniqueConstraint(condition=models.Q(('task__isnull', False)), fields=('task', 'user'), name='unique_task_earning_per_user'),
        ),
    ]
