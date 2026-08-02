"""Turn free-text target dates into real dates.

The old field was a CharField holding whatever someone typed — "Jul 18", "TBD",
"end of August", "". Only some of that is a date, and none of it carries a year.

What this does:

* parses the formats actually used (``Jul 18``, ``18 Jul``, ``2026-07-18``,
  ``18/07/2026``);
* infers the missing year from the project's ``created_at``, rolling forward a
  year when the result would otherwise land before the brief was even posted
  (a project created in December with a target of "Jan 10" means next January);
* leaves anything it can't parse — including "TBD" — as **null**.

Null is the honest answer for an unparseable value, and it matters: "no date
agreed" and "the date has passed" are different states, and on-time reporting
excludes the first rather than counting it as a miss.
"""
from datetime import datetime

from django.db import migrations

# Formats seen in the wild, most specific first.
FORMATS = [
    ("%Y-%m-%d", True),   # 2026-07-18 — year included
    ("%d/%m/%Y", True),
    ("%d %b %Y", True),
    ("%b %d %Y", True),
    ("%b %d", False),     # Jul 18 — no year
    ("%d %b", False),
    ("%B %d", False),     # July 18
    ("%d %B", False),
]

NON_DATES = {"", "tbd", "n/a", "na", "none", "-", "—", "flexible", "asap"}


def parse(raw, created_at):
    text = (raw or "").strip().rstrip(",.")
    if text.lower() in NON_DATES:
        return None

    for fmt, has_year in FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if has_year:
            return parsed.date()
        # No year in the string: assume the project's own year, and roll forward
        # if that would put the target before the brief existed.
        candidate = parsed.replace(year=created_at.year).date()
        if candidate < created_at.date():
            candidate = candidate.replace(year=created_at.year + 1)
        return candidate
    return None


def forwards(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    for project in Project.objects.exclude(target_date="").iterator():
        parsed = parse(project.target_date, project.created_at)
        if parsed:
            project.target_date_new = parsed
            project.save(update_fields=["target_date_new"])


def backwards(apps, schema_editor):
    """Write the dates back as text, so the reverse isn't lossy for what parsed."""
    Project = apps.get_model("projects", "Project")
    for project in Project.objects.filter(target_date_new__isnull=False).iterator():
        project.target_date = project.target_date_new.strftime("%b %-d")
        project.save(update_fields=["target_date"])


class Migration(migrations.Migration):

    dependencies = [("projects", "0014_add_target_date_field")]

    operations = [migrations.RunPython(forwards, backwards)]
