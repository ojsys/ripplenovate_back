"""Rename the `developer` earning kind to `expert`.

This rewrites ledger rows. `Project.payout_split()` looks earnings up by their
`kind` string, so if this migration and the code diverge a completed project
silently reports projected figures instead of what was actually paid. Ships with
accounts/0006 and projects/0006.
"""
from django.db import migrations, models


def developer_to_expert(apps, schema_editor):
    Earning = apps.get_model("payments", "Earning")
    Earning.objects.filter(kind="developer").update(kind="expert")


def expert_to_developer(apps, schema_editor):
    Earning = apps.get_model("payments", "Earning")
    Earning.objects.filter(kind="expert").update(kind="developer")


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0004_withdrawal_bank_code_withdrawal_failure_reason_and_more"),
        ("projects", "0006_project_developer_to_expert"),
    ]

    operations = [
        migrations.RunPython(developer_to_expert, expert_to_developer),
        migrations.AlterField(
            model_name="earning",
            name="kind",
            field=models.CharField(
                choices=[
                    ("expert", "Expert share"),
                    ("delivery_lead", "Delivery lead share"),
                ],
                max_length=20,
            ),
        ),
    ]
