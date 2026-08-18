"""Turn every client's `company` string into a real organisation.

`User.company` was free text, so "Acme Ltd", "acme ltd" and "ACME LTD" were
three different companies and none of them was an entity. This creates one
`Organisation` per distinct name, folds the case variants together, makes each
client an owner of theirs, and points their projects at it.

**Every client gets one, including sole traders with the field left blank.**
A personal organisation named after them is odd to look at and much better to
program against: one code path afterwards instead of "an org, or else the old
behaviour" threaded through access control, billing and reporting.

Deliberately conservative about matching. Names are folded on a
case-insensitive, whitespace-collapsed comparison and nothing cleverer — no
stripping of "Ltd"/"Inc", no fuzzy matching. Wrongly merging two real companies
would put one buyer's briefs and budgets in front of another, which is a far
worse outcome than leaving two near-duplicate rows for an admin to tidy.

Reversible: the reverse detaches projects and deletes the rows, leaving
`User.company` exactly as it was. Nothing here writes to that field.
"""
import re

from django.db import migrations
from django.utils.text import slugify


def _fold(name):
    """The comparison key: case-insensitive, whitespace-collapsed."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _unique_slug(Organisation, name, taken):
    base = slugify(name)[:150] or "org"
    slug = base
    n = 2
    while slug in taken or Organisation.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    taken.add(slug)
    return slug


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Organisation = apps.get_model("accounts", "Organisation")
    OrganisationMember = apps.get_model("accounts", "OrganisationMember")
    Project = apps.get_model("projects", "Project")

    taken_slugs = set()
    # Seeded from what's already there, not started empty. Django won't re-run
    # a migration on its own, but a reversed-then-reapplied one would, and a
    # backfill that duplicates every company on a second pass is a bad thing to
    # discover on a production database.
    by_key = {_fold(o.name): o for o in Organisation.objects.all()}

    clients = User.objects.filter(role="client").order_by("id")
    for client in clients:
        # Already placed — by an earlier pass, or by signing up after this
        # shipped. Nothing to do, and certainly nothing to create.
        if OrganisationMember.objects.filter(user=client).exists():
            Project.objects.filter(
                client=client, organisation__isnull=True
            ).update(
                organisation_id=OrganisationMember.objects
                .filter(user=client).values_list("organisation_id", flat=True)
                .first()
            )
            continue

        key = _fold(client.company)
        if key:
            org = by_key.get(key)
            if org is None:
                # First spelling seen wins the display name — arbitrary, but
                # stable, and an admin can rename it.
                org = Organisation.objects.create(
                    name=client.company.strip(),
                    slug=_unique_slug(Organisation, client.company.strip(), taken_slugs),
                )
                by_key[key] = org
        else:
            # A sole trader. Their own organisation, named after them, so
            # everything downstream has exactly one shape to handle.
            label = client.full_name.strip() or client.email
            org = Organisation.objects.create(
                name=label,
                slug=_unique_slug(Organisation, label, taken_slugs),
            )

        OrganisationMember.objects.get_or_create(
            organisation=org, user=client, defaults={"role": "owner"}
        )
        Project.objects.filter(client=client, organisation__isnull=True).update(
            organisation=org
        )


def unbackfill(apps, schema_editor):
    Organisation = apps.get_model("accounts", "Organisation")
    OrganisationMember = apps.get_model("accounts", "OrganisationMember")
    Project = apps.get_model("projects", "Project")

    # Projects first: `organisation` is PROTECT, so the rows can't go while
    # anything still points at them.
    Project.objects.update(organisation=None)
    OrganisationMember.objects.all().delete()
    Organisation.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0027_project_organisation"),
        ("accounts", "0020_organisation_organisationmember"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
