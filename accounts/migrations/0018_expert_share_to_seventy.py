"""Raise the expert share to 70%.

The split is: expert 70, delivery lead 15, business developer 5 when there is
one. The platform is the remainder, so it takes 15% on a direct project and 10%
on a sourced one — no field needs to change for that, because the platform's
cut has never been stored.

Changing `DEFAULT_EXPERT_SHARE` alone would not have moved anything: the default
applies to rows that don't exist yet, and every deployment already has its one
`SiteSettings` row saved with the old figure.

**Only a row still holding the previous default is touched.** If an admin has
deliberately set some other number, that was a decision and this migration is
not entitled to overrule it — a data migration that silently overwrites
configured values is how a deploy quietly changes what people get paid.

Per-project overrides are likewise left alone. An override is an explicit choice
about one piece of work, usually a large or unusual build, and re-deriving those
from a new default would restate deals that were already agreed.

Nothing already paid moves. Earnings snapshot their percentage at approval, so
every completed project keeps the split it was settled under.
"""
from decimal import Decimal

from django.db import migrations

OLD = Decimal("60.00")
NEW = Decimal("70.00")


def raise_share(apps, schema_editor):
    SiteSettings = apps.get_model("accounts", "SiteSettings")
    SiteSettings.objects.filter(expert_share_percent=OLD).update(
        expert_share_percent=NEW
    )


def lower_share(apps, schema_editor):
    SiteSettings = apps.get_model("accounts", "SiteSettings")
    SiteSettings.objects.filter(expert_share_percent=NEW).update(
        expert_share_percent=OLD
    )


class Migration(migrations.Migration):

    dependencies = [("accounts", "0017_notification")]

    operations = [
        migrations.RunPython(raise_share, lower_share),
    ]
