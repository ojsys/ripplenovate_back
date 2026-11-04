"""Payouts reaching their final state, with or without the webhook.

The bug these pin down: Paystack answers a new transfer with `pending`, and the
only thing that ever moved it on was the `transfer.success` webhook. When that
didn't arrive, a payout that had landed sat at "Sending" forever — still in
the lead's queue, still offering to be sent again or rejected.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from payments import transfers
from payments.models import Withdrawal

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class PayoutTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.lead = User.objects.create_user(
            "trlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "trexpert@ril.dev", "x", full_name="An Expert",
            role=User.Role.EXPERT, bank_name="GTBank", bank_code="058",
            bank_account_number="0123456789", bank_account_name="An Expert",
            paystack_recipient_code="RCP_1")

    def withdrawal(self, status=Withdrawal.Status.REQUESTED, **kw):
        return Withdrawal.objects.create(
            user=self.expert, reference=kw.pop("reference", "WD-1"),
            amount_usd=Decimal("100"), currency="NGN", amount_subunit=15000000,
            bank_name="GTBank", bank_account_number="0123456789",
            bank_account_name="An Expert", status=status, **kw)

    def sending(self):
        return self.withdrawal(Withdrawal.Status.PROCESSING,
                               transfer_code="TRF_1", transfer_reference="WD-1")



@override_settings(PAYSTACK_TRANSFERS_ENABLED=True)
class PayoutReconcileTests(PayoutTestBase):
    # -- the page heals a stuck payout ---------------------------------------

    def test_loading_the_page_settles_a_payout_paystack_has_finished(self):
        w = self.sending()
        with mock.patch.object(transfers, "_get",
                               return_value={"status": "success", "reference": "WD-1"}):
            data = as_user(self.lead).get("/api/earnings").data
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PAID)
        self.assertEqual(data["payout_queue"], [])
        # The earner hears about it exactly as the webhook would have told them.
        self.assertIn(self.expert.email, [r for m in mail.outbox for r in m.to])

    def test_the_earner_sees_their_own_payout_settle(self):
        w = self.sending()
        with mock.patch.object(transfers, "_get",
                               return_value={"status": "success", "reference": "WD-1"}):
            data = as_user(self.expert).get("/api/earnings").data
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PAID)
        self.assertEqual(data["withdrawals"][0]["status"], "paid")

    def test_a_still_pending_transfer_stays_sending(self):
        w = self.sending()
        with mock.patch.object(transfers, "_get",
                               return_value={"status": "pending", "reference": "WD-1"}):
            as_user(self.lead).get("/api/earnings")
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PROCESSING)

    def test_paystack_being_down_does_not_break_the_page(self):
        self.sending()
        with mock.patch.object(transfers, "_get", side_effect=Exception("timeout")):
            resp = as_user(self.lead).get("/api/earnings")
        self.assertEqual(resp.status_code, 200)

    def test_it_does_not_hammer_paystack_on_every_refresh(self):
        self.sending()
        with mock.patch.object(transfers, "_get",
                               return_value={"status": "pending"}) as get:
            as_user(self.lead).get("/api/earnings")
            as_user(self.lead).get("/api/earnings")
        self.assertEqual(get.call_count, 1)

    @override_settings(PAYSTACK_TRANSFERS_ENABLED=False)
    def test_nothing_is_asked_with_transfers_off(self):
        self.sending()
        with mock.patch.object(transfers, "_get") as get:
            as_user(self.lead).get("/api/earnings")
        get.assert_not_called()

    # -- settling a payout that is already sending ---------------------------

    def test_sending_again_checks_instead_of_resending(self):
        w = self.sending()
        with mock.patch.object(transfers, "_post") as post, \
             mock.patch.object(transfers, "_get",
                               return_value={"status": "success", "reference": "WD-1"}):
            resp = as_user(self.lead).post(
                f"/api/withdrawals/{w.id}/settle", {"status": "paid"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["status"], "paid")
        post.assert_not_called()

    def test_a_payout_in_flight_cannot_be_rejected(self):
        w = self.sending()
        resp = as_user(self.lead).post(
            f"/api/withdrawals/{w.id}/settle",
            {"status": "rejected", "note": "nope"}, format="json")
        self.assertEqual(resp.status_code, 400)
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PROCESSING)

    def test_a_fresh_send_still_goes_through(self):
        w = self.withdrawal()
        with mock.patch.object(transfers, "_post", return_value={
                "status": "success", "reference": "WD-1", "transfer_code": "TRF_9"}):
            resp = as_user(self.lead).post(
                f"/api/withdrawals/{w.id}/settle", {"status": "paid"}, format="json")
        self.assertEqual(resp.data["status"], "paid")

    def test_the_status_check_endpoint_settles_it(self):
        w = self.sending()
        with mock.patch.object(transfers, "_get",
                               return_value={"status": "success", "reference": "WD-1"}):
            resp = as_user(self.lead).post(f"/api/withdrawals/{w.id}/sync")
        self.assertEqual(resp.data["status"], "paid")


@override_settings(PAYSTACK_TRANSFERS_ENABLED=True)
class ScheduledSyncTests(PayoutTestBase):
    """No webhook: the scheduled job is what settles a payout nobody is watching."""

    def run_sync(self, body):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with mock.patch.object(transfers, "_get", return_value=body) as get:
            call_command("sync_payouts", stdout=out)
        return out.getvalue(), get

    def test_it_settles_a_payout_with_nobody_looking(self):
        w = self.sending()
        out, _ = self.run_sync({"status": "success", "reference": "WD-1"})
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PAID)
        self.assertIn("1 changed", out)
        self.assertIn(self.expert.email, [r for m in mail.outbox for r in m.to])

    def test_it_is_not_throttled_by_a_recent_page_view(self):
        self.sending()
        with mock.patch.object(transfers, "_get", return_value={"status": "pending"}):
            as_user(self.lead).get("/api/earnings")
        _, get = self.run_sync({"status": "pending"})
        self.assertEqual(get.call_count, 1)

    def test_a_reversed_payout_goes_back_to_the_balance(self):
        from django.utils import timezone

        w = self.withdrawal(Withdrawal.Status.PAID, transfer_code="TRF_1",
                            transfer_reference="WD-1", processed_at=timezone.now())
        self.run_sync({"status": "reversed", "reference": "WD-1"})
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.FAILED)

    def test_reconfirming_keeps_when_it_was_paid(self):
        from datetime import timedelta

        from django.utils import timezone

        landed = timezone.now() - timedelta(days=1)
        w = self.withdrawal(Withdrawal.Status.PAID, transfer_code="TRF_1",
                            transfer_reference="WD-1", processed_at=landed)
        self.run_sync({"status": "success", "reference": "WD-1"})
        w.refresh_from_db()
        self.assertEqual(w.processed_at, landed)

    def test_old_and_hand_recorded_payouts_are_left_alone(self):
        from datetime import timedelta

        from django.utils import timezone

        self.withdrawal(Withdrawal.Status.PAID, reference="WD-OLD",
                        transfer_reference="WD-OLD",
                        processed_at=timezone.now() - timedelta(days=10))
        self.withdrawal(Withdrawal.Status.PAID, reference="WD-HAND",
                        processed_at=timezone.now())
        _, get = self.run_sync({"status": "success"})
        get.assert_not_called()

    @override_settings(PAYSTACK_TRANSFERS_ENABLED=False)
    def test_it_does_nothing_with_transfers_off(self):
        self.sending()
        out, get = self.run_sync({"status": "success"})
        get.assert_not_called()
        self.assertIn("off", out)
