"""Retire the free-text target date and put the real one in its place.

Runs after 0015 has copied everything parseable across, so the drop only loses
values that were never dates ("TBD", "flexible") — which are correctly null now.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [("projects", "0015_parse_target_dates")]

    operations = [
        migrations.RemoveField(model_name="project", name="target_date"),
        migrations.RenameField(
            model_name="project", old_name="target_date_new", new_name="target_date",
        ),
    ]
