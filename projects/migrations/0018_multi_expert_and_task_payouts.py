"""Add the multi-expert team and the task lifecycle fields.

Purely additive. `Task.done` stays for now so 0019 has something to read when it
translates it into `status`; 0020 drops it once the data has moved.
"""
import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0017_attachment_files'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='experts',
            field=models.ManyToManyField(blank=True, help_text='The experts delivering this project. Tasks can only be assigned to someone on this list.', limit_choices_to={'role': 'expert'}, related_name='assigned_projects', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='task',
            name='amount_usd',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text="What the assignee earns when this task is approved. Comes out of the project's expert share.", max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal('0'))], verbose_name='Amount (USD)'),
        ),
        migrations.AddField(
            model_name='task',
            name='approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='task',
            name='approved_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='approved_tasks', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='task',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
        migrations.AddField(
            model_name='task',
            name='description',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='task',
            name='status',
            field=models.CharField(choices=[('todo', 'To do'), ('submitted', 'Submitted for review'), ('changes', 'Changes requested'), ('approved', 'Approved')], default='todo', max_length=10),
        ),
        migrations.AddField(
            model_name='task',
            name='submitted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='project',
            name='expert',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='primary_expert_projects', to=settings.AUTH_USER_MODEL, verbose_name='Primary expert'),
        ),
        migrations.AlterField(
            model_name='task',
            name='assignee',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.CheckConstraint(condition=models.Q(('amount_usd__gte', Decimal('0'))), name='task_amount_not_negative'),
        ),
    ]
