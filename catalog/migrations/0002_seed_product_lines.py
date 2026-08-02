"""Create the starting catalogue of product lines and services.

All five lines are created; which ones a client can actually post into is
controlled by `is_active` in the admin, so opening a discipline is a switch
rather than a deploy. Idempotent — it only creates what isn't there.
"""
from django.db import migrations

from catalog.seed_data import PRODUCT_LINES


def create_catalogue(apps, schema_editor):
    ProductLine = apps.get_model("catalog", "ProductLine")
    Service = apps.get_model("catalog", "Service")

    for order, (slug, name, tagline, accent, icon, services) in enumerate(PRODUCT_LINES):
        line, _ = ProductLine.objects.get_or_create(
            slug=slug,
            defaults={
                "name": name, "tagline": tagline, "accent": accent,
                "icon": icon, "order": order, "is_active": True,
            },
        )
        for s_order, (s_name, s_desc, s_timeline) in enumerate(services):
            Service.objects.get_or_create(
                product_line=line, name=s_name,
                defaults={
                    "description": s_desc, "typical_timeline": s_timeline,
                    "order": s_order, "is_active": True,
                },
            )


def drop_catalogue(apps, schema_editor):
    ProductLine = apps.get_model("catalog", "ProductLine")
    ProductLine.objects.filter(
        slug__in=[line[0] for line in PRODUCT_LINES]
    ).delete()  # cascades to services


class Migration(migrations.Migration):

    dependencies = [("catalog", "0001_product_line_and_service")]

    operations = [migrations.RunPython(create_catalogue, drop_catalogue)]
