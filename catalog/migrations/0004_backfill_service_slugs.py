"""Give every existing service a slug.

`save()` derives one, but that only fires when a row is written — and these rows
were written long before the field existed. Without this the public service
pages would silently skip every service on a live deployment.

Collisions within a line are resolved by suffixing rather than by refusing:
two services whose names slugify identically is a data-tidying problem for an
admin, not a reason for a migration to stop.
"""
from django.db import migrations
from django.utils.text import slugify


def fill(apps, schema_editor):
    Service = apps.get_model("catalog", "Service")
    taken = set()
    for service in Service.objects.order_by("product_line_id", "order", "id"):
        if service.slug:
            taken.add((service.product_line_id, service.slug))
            continue
        base = slugify(service.name)[:120] or f"service-{service.pk}"
        slug, n = base, 2
        while (service.product_line_id, slug) in taken:
            slug = f"{base}-{n}"
            n += 1
        taken.add((service.product_line_id, slug))
        service.slug = slug
        service.save(update_fields=["slug"])


def clear(apps, schema_editor):
    apps.get_model("catalog", "Service").objects.update(slug="")


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_service_slug_service_unique_service_slug_per_line"),
    ]

    operations = [migrations.RunPython(fill, clear)]
