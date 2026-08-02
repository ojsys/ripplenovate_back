"""Put every existing project and person into a product line.

Two things would otherwise break the moment line scoping goes live:

* an existing brief has no `product_line`, so no lead's queue would show it;
* an existing delivery lead belongs to no line, so their board would empty out.

Both are backfilled here. Projects map through their old free-text `category`;
anything unrecognised (and every existing lead and expert) goes to Software &
Web, which is what the platform delivered before this change.
"""
from django.db import migrations

from catalog.seed_data import LEGACY_CATEGORY_MAP

FALLBACK_LINE = "software-web"


def backfill(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    ProductLine = apps.get_model("catalog", "ProductLine")
    Service = apps.get_model("catalog", "Service")
    User = apps.get_model("accounts", "User")

    lines = {line.slug: line for line in ProductLine.objects.all()}
    fallback = lines.get(FALLBACK_LINE)
    if not fallback:
        return  # catalogue missing (fresh DB, nothing to backfill)

    def service_for(line, name):
        return Service.objects.filter(product_line=line, name=name).first()

    for project in Project.objects.filter(product_line__isnull=True):
        slug, service_name = LEGACY_CATEGORY_MAP.get(
            project.category, (FALLBACK_LINE, None)
        )
        line = lines.get(slug, fallback)
        project.product_line = line
        project.service = service_for(line, service_name) if service_name else None
        project.save(update_fields=["product_line", "service"])

    # Everyone who delivers work today delivered software.
    for user in User.objects.filter(role__in=["delivery_lead", "expert"]):
        if not user.product_lines.exists():
            user.product_lines.add(fallback)


def unbackfill(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Project.objects.update(product_line=None, service=None)


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0008_product_lines"),
        ("accounts", "0008_product_lines"),
        ("catalog", "0002_seed_product_lines"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
