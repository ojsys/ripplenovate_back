"""Rename the `developer` role to `expert` — data + schema, in one release.

The platform delivers more than software now, so the talent role is a "Project
Delivery Expert". The stored value is migrated rather than aliased: carrying
role="developer" while the whole UI says "Expert" is a permanent trap for anyone
reading the data later.

Ships together with payments/0005 (Earning.kind) and projects/0006 (Project.expert)
— `payout_split()` reads the earning kind by literal string, so the three have to
move as one.
"""
from django.db import migrations, models


def developer_to_expert(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(role="developer").update(role="expert")


def expert_to_developer(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(role="expert").update(role="developer")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_user_bank_code_user_paystack_recipient_code"),
    ]

    operations = [
        migrations.RunPython(developer_to_expert, expert_to_developer),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("client", "Client"),
                    ("delivery_lead", "Delivery Lead"),
                    ("expert", "Project Delivery Expert"),
                ],
                default="client",
                max_length=20,
            ),
        ),
        migrations.RenameField(
            model_name="sitesettings",
            old_name="developer_share_percent",
            new_name="expert_share_percent",
        ),
    ]
