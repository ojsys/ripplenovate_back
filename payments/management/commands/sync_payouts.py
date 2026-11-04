"""Settle payouts by asking Paystack where each transfer stands.

This platform doesn't receive Paystack transfer webhooks — the Paystack account
is shared with other products and it has only one webhook URL — so this job is
how a payout reaches Paid (or Failed) when nobody has the Earnings page open.
Run it every few minutes:

    python manage.py sync_payouts

Doing nothing is the normal outcome. It asks about payouts still sending, plus
ones paid in the last few days in case the bank reversed them.
"""
from django.core.management.base import BaseCommand

from payments import transfers


class Command(BaseCommand):
    help = "Re-check in-flight and recently paid payouts with Paystack."

    def handle(self, *args, **options):
        if not transfers.transfers_enabled():
            self.stdout.write("Paystack transfers are off — nothing to sync.")
            return
        checked, changed = transfers.sync_all()
        self.stdout.write(
            f"Checked {len(checked)} payout(s); {len(changed)} changed.")
        for w in changed:
            self.stdout.write(f"  · {w.reference}: {w.get_status_display()}")
