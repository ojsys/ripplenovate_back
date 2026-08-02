"""Rename Project.developer → Project.expert and its payout override.

Ships with accounts/0006 and payments/0005 — see the note there.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0005_project_delivery_lead_share_percent_and_more"),
        ("accounts", "0006_developer_role_to_expert"),
    ]

    operations = [
        migrations.RenameField(
            model_name="project", old_name="developer", new_name="expert",
        ),
        migrations.RenameField(
            model_name="project",
            old_name="developer_share_percent",
            new_name="expert_share_percent",
        ),
    ]
