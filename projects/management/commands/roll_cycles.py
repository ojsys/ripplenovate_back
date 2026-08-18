"""Raise the next month's cycle for every live retainer.

Run daily. Doing nothing is the normal outcome — a cycle is only raised in the
week before its period starts, so most days this logs a run with zero created
and exits.

    python manage.py roll_cycles --dry-run    # say what would happen
    python manage.py roll_cycles              # actually do it

Every pass writes a `CycleRun`, visible in the Django admin, because this is
the one job that creates billable records with nobody watching.
"""
from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date

from projects import engagements


class Command(BaseCommand):
    help = "Generate the next billing cycle for active engagements."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be raised without creating anything.",
        )
        parser.add_argument(
            "--on", type=str, default=None,
            help="Pretend today is this date (YYYY-MM-DD). For checking ahead.",
        )
        parser.add_argument(
            "--limit", type=int, default=engagements.MAX_PER_RUN,
            help="Stop after this many cycles. A safety cap, not a target.",
        )

    def handle(self, *args, **options):
        on = parse_date(options["on"]) if options["on"] else None
        if options["on"] and on is None:
            self.stderr.write(self.style.ERROR("--on must be YYYY-MM-DD"))
            return

        entry, created = engagements.run(
            dry_run=options["dry_run"],
            on=on,
            limit=options["limit"],
            triggered_by="manage.py roll_cycles",
        )
        prefix = "Would raise" if options["dry_run"] else "Raised"
        self.stdout.write(
            f"{prefix} {len(created)} cycle(s); skipped {entry.skipped_count}."
        )
        for line in created:
            self.stdout.write(f"  · {line}")
        if entry.detail and not created:
            self.stdout.write(entry.detail)
