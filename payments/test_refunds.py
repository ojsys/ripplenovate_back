"""Cancellation, refunds and the reserve (G2).

The platform collected the whole quote up front and released expert payments as
tasks were approved — with no way to give any of it back. The exposure was
known and written down; what was missing was the mechanism.

The invariant these tests exist to protect: **a refund never debits an expert.**
Money comes from what the project still holds, then the reserve, then the
platform's own pocket. Nobody who has been paid for approved work is ever asked
to return it, because "the money is already in the building" is the promise the
whole delivery model rests on.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SiteSettings
from catalog.models import ProductLine
from payments import refunds as refund_service
from payments.models import Earning, Payment, Refund, ReserveEntry
from projects.models import Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class RefundTestBase(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "reflead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "refexpert@ril.dev", "x", full_name="An Expert",
            role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "refclient@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)
        self.admin = User.objects.create_superuser(
            "refboss@ril.team", "x", full_name="An Admin")

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.expert)
        self.pay(Decimal("5000"))

    def pay(self, usd):
        return Payment.objects.create(
            project=self.project, reference=f"ref-{timezone.now().timestamp()}",
            amount_subunit=int(usd * 100), currency="USD", usd_total=usd,
            status=Payment.Status.SUCCESS, paid_at=timezone.now())

    def approve_task(self, amount):
        """An expert paid for approved work — money already out of the door."""
        task = Task.objects.create(
            project=self.project, title="A chunk of work", assignee=self.expert,
            amount_usd=Decimal(amount), status=Task.Status.APPROVED)
        Earning.objects.create(
            user=self.expert, project=self.project, task=task,
            kind=Earning.Kind.EXPERT,
            share_percent=Decimal("10"), amount_usd=Decimal(amount))
        return task

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"


class CrossAppContractTests(TestCase):
    def test_payment_success_literal_matches_the_enum(self):
        """`Project.collected_usd` compares against the string "success" because
        it can't import Payment (payments already imports projects). This is the
        guard on that: rename the enum value and this fails, instead of every
        refund silently deciding the client paid nothing."""
        self.assertEqual(Payment.Status.SUCCESS, "success")


class RefundFundingTests(RefundTestBase):
    """Where the money comes from, in order."""

    def test_a_refund_on_untouched_money_costs_the_reserve_nothing(self):
        plan = refund_service.plan_funding(self.project, Decimal("1000"))
        self.assertEqual(plan["from_held_usd"], Decimal("1000.00"))
        self.assertEqual(plan["from_reserve_usd"], Decimal("0.00"))
        self.assertEqual(plan["absorbed_usd"], Decimal("0.00"))

    def test_released_money_reduces_what_can_be_refunded_freely(self):
        self.approve_task(3000)
        self.assertEqual(self.project.free_refund_usd, Decimal("2000.00"))
        self.assertEqual(self.project.refundable_usd, Decimal("5000.00"),
                         "released money limits the painless refund, not the possible one")

    def test_beyond_what_is_held_it_draws_on_the_reserve(self):
        self.approve_task(4000)
        ReserveEntry.objects.create(
            kind=ReserveEntry.Kind.CONTRIBUTION, amount_usd=Decimal("800"),
            project=self.project)
        plan = refund_service.plan_funding(self.project, Decimal("1500"))
        self.assertEqual(plan["from_held_usd"], Decimal("1000.00"))
        self.assertEqual(plan["from_reserve_usd"], Decimal("500.00"))
        self.assertEqual(plan["absorbed_usd"], Decimal("0.00"))

    def test_an_empty_reserve_means_the_platform_absorbs_it(self):
        """Refused would be worse. A business that can't make a customer whole
        until an internal pot refills doesn't have a refund policy."""
        self.approve_task(4500)
        plan = refund_service.plan_funding(self.project, Decimal("2000"))
        self.assertEqual(plan["from_held_usd"], Decimal("500.00"))
        self.assertEqual(plan["from_reserve_usd"], Decimal("0.00"))
        self.assertEqual(plan["absorbed_usd"], Decimal("1500.00"))

    def test_the_split_always_adds_up(self):
        self.approve_task(4000)
        ReserveEntry.objects.create(
            kind=ReserveEntry.Kind.CONTRIBUTION, amount_usd=Decimal("250"),
            project=self.project)
        for amount in ("0.01", "999.99", "1000", "1234.56", "5000"):
            with self.subTest(amount=amount):
                plan = refund_service.plan_funding(self.project, Decimal(amount))
                total = (plan["from_held_usd"] + plan["from_reserve_usd"]
                         + plan["absorbed_usd"])
                self.assertEqual(total, Decimal(amount))


class ExpertProtectionTests(RefundTestBase):
    """The invariant the whole design exists to keep."""

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_a_full_refund_never_debits_an_expert(self):
        self.approve_task(3000)
        before = Earning.objects.filter(user=self.expert).count()

        response = as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "5000", "reason": "The whole thing failed."},
            format="json")
        self.assertEqual(response.status_code, 201, response.data)

        self.assertEqual(Earning.objects.filter(user=self.expert).count(), before)
        self.assertEqual(
            Earning.objects.filter(user=self.expert).first().amount_usd,
            Decimal("3000.00"))

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_no_negative_earning_can_exist_after_any_refund(self):
        self.approve_task(4800)
        as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "5000", "reason": "Total failure."}, format="json")
        self.assertFalse(Earning.objects.filter(amount_usd__lt=0).exists())

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_the_experts_balance_is_untouched(self):
        """`available` is clamped at zero, so a clawback would silently hide a
        debt rather than show one. It stays correct only while nothing debits."""
        from payments import earnings as earnings_service

        self.approve_task(3000)
        before = earnings_service.available_balance(self.expert)
        as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "5000", "reason": "Failed."}, format="json")
        self.assertEqual(earnings_service.available_balance(self.expert), before)


class RefundPermissionTests(RefundTestBase):
    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_the_projects_lead_can_refund_under_the_threshold(self):
        response = as_user(self.lead).post(
            self.url("/refunds"),
            {"amount_usd": "200", "reason": "Goodwill."}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Refund.objects.get().status, Refund.Status.PROCESSED)

    def test_a_large_refund_waits_for_an_admin(self):
        response = as_user(self.lead).post(
            self.url("/refunds"),
            {"amount_usd": "2500", "reason": "Big problem."}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.REQUESTED)
        self.assertIsNone(refund.approved_by)

    def test_the_threshold_is_configurable(self):
        row = SiteSettings.load()
        row.refund_admin_threshold_usd = Decimal("50")
        row.save(update_fields=["refund_admin_threshold_usd"])
        as_user(self.lead).post(
            self.url("/refunds"),
            {"amount_usd": "200", "reason": "Goodwill."}, format="json")
        self.assertEqual(Refund.objects.get().status, Refund.Status.REQUESTED)

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_an_admin_is_never_held_to_the_threshold(self):
        response = as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "4000", "reason": "Approved by me."}, format="json")
        self.assertEqual(Refund.objects.get().status, Refund.Status.PROCESSED)

    def test_the_client_cannot_refund_themselves(self):
        self.assertEqual(
            as_user(self.customer).post(
                self.url("/refunds"),
                {"amount_usd": "100", "reason": "I want my money."},
                format="json").status_code, 403)

    def test_the_expert_cannot_issue_a_refund(self):
        self.assertEqual(
            as_user(self.expert).post(
                self.url("/refunds"),
                {"amount_usd": "100", "reason": "…"}, format="json").status_code, 403)

    def test_another_lead_cannot_refund_someone_elses_project(self):
        other = User.objects.create_user(
            "refother@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        other.product_lines.add(self.line)
        self.assertIn(
            as_user(other).post(
                self.url("/refunds"),
                {"amount_usd": "100", "reason": "…"}, format="json").status_code,
            (403, 404))

    def test_the_client_can_still_read_their_own_refunds(self):
        response = as_user(self.customer).get(self.url("/refunds"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("refundable_usd", response.data)


class RefundLimitTests(RefundTestBase):
    def test_a_reason_is_required(self):
        self.assertEqual(
            as_user(self.admin).post(
                self.url("/refunds"), {"amount_usd": "100", "reason": "  "},
                format="json").status_code, 400)

    def test_zero_and_negative_are_refused(self):
        for amount in ("0", "-50"):
            with self.subTest(amount=amount):
                self.assertEqual(
                    as_user(self.admin).post(
                        self.url("/refunds"),
                        {"amount_usd": amount, "reason": "…"},
                        format="json").status_code, 400)

    def test_you_cannot_refund_more_than_the_client_paid(self):
        response = as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "6000", "reason": "Too much."}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("left to refund", str(response.data))

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_refunds_cannot_add_up_past_what_was_paid(self):
        for _ in range(2):
            as_user(self.admin).post(
                self.url("/refunds"),
                {"amount_usd": "2500", "reason": "Part."}, format="json")
        third = as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "1", "reason": "One more."}, format="json")
        self.assertEqual(third.status_code, 400)
        self.project.refresh_from_db()
        self.assertEqual(self.project.refunded_usd, Decimal("5000.00"))

    def test_an_unpaid_project_cannot_be_refunded(self):
        unpaid = Project.objects.create(
            title="Unpaid", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.QUOTED, quote_usd=1000)
        self.assertEqual(
            as_user(self.admin).post(
                f"/api/projects/{unpaid.id}/refunds",
                {"amount_usd": "100", "reason": "…"}, format="json").status_code, 400)

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_a_rejected_refund_frees_the_money_again(self):
        as_user(self.lead).post(
            self.url("/refunds"),
            {"amount_usd": "3000", "reason": "Maybe."}, format="json")
        refund = Refund.objects.get()
        as_user(self.admin).post(
            f"/api/refunds/{refund.id}/decide",
            {"decision": "reject", "reason": "Not warranted."}, format="json")
        self.project.refresh_from_db()
        self.assertEqual(self.project.refunded_usd, Decimal("0.00"))
        self.assertEqual(self.project.refundable_usd, Decimal("5000.00"))


class ReserveTests(RefundTestBase):
    def test_completing_a_project_sets_aside_the_platform_slice(self):
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        as_user(self.customer).post(self.url("/approve"))

        entry = ReserveEntry.objects.get(kind=ReserveEntry.Kind.CONTRIBUTION)
        self.project.refresh_from_db()
        expected = (self.project.collected_usd - self.project.released_usd) * Decimal("0.05")
        self.assertEqual(entry.amount_usd, expected.quantize(Decimal("0.01")))

    def test_it_is_set_aside_exactly_once(self):
        """Crediting runs lazily on every earnings read, so this has to hold."""
        from payments import earnings as earnings_service

        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        as_user(self.customer).post(self.url("/approve"))
        for _ in range(3):
            earnings_service.summary(self.lead)
            refund_service.contribute(self.project)
        self.assertEqual(
            ReserveEntry.objects.filter(
                kind=ReserveEntry.Kind.CONTRIBUTION).count(), 1)

    def test_nothing_is_set_aside_before_completion(self):
        refund_service.contribute(self.project)
        self.assertEqual(ReserveEntry.objects.count(), 0)

    def test_a_zero_percent_reserve_writes_nothing(self):
        row = SiteSettings.load()
        row.reserve_percent = Decimal("0")
        row.save(update_fields=["reserve_percent"])
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        as_user(self.customer).post(self.url("/approve"))
        self.assertEqual(ReserveEntry.objects.count(), 0)

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_the_balance_reconciles_against_its_own_rows(self):
        ReserveEntry.objects.create(
            kind=ReserveEntry.Kind.CONTRIBUTION, amount_usd=Decimal("900"),
            project=self.project)
        self.assertEqual(refund_service.reserve_balance(), Decimal("900.00"))

        self.approve_task(4500)
        as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "1000", "reason": "Failed."}, format="json")

        refund = Refund.objects.get()
        self.assertEqual(refund.funded_from_held_usd, Decimal("500.00"))
        self.assertEqual(refund.funded_from_reserve_usd, Decimal("500.00"))
        self.assertEqual(refund.absorbed_usd, Decimal("0.00"))
        self.assertEqual(refund_service.reserve_balance(), Decimal("400.00"))

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_a_shortfall_is_recorded_not_hidden(self):
        self.approve_task(5000)
        as_user(self.admin).post(
            self.url("/refunds"),
            {"amount_usd": "1200", "reason": "Failed."}, format="json")
        refund = Refund.objects.get()
        self.assertEqual(refund.absorbed_usd, Decimal("1200.00"))
        self.assertEqual(refund_service.reserve_balance(), Decimal("0.00"))

    def test_only_an_admin_sees_the_queue_and_the_reserve(self):
        self.assertEqual(
            as_user(self.lead).get("/api/refunds/queue").status_code, 403)
        response = as_user(self.admin).get("/api/refunds/queue")
        self.assertEqual(response.status_code, 200)
        self.assertIn("reserve_balance_usd", response.data)


class CancellationTests(RefundTestBase):
    def test_a_client_can_cancel_before_paying(self):
        unpaid = Project.objects.create(
            title="Unpaid", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.QUOTED, quote_usd=1000)
        response = as_user(self.customer).post(
            f"/api/projects/{unpaid.id}/cancel",
            {"reason": "Budget pulled."}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        unpaid.refresh_from_db()
        self.assertEqual(unpaid.stage, Project.Stage.CANCELLED)
        self.assertEqual(unpaid.cancelled_by_id, self.customer.id)
        self.assertEqual(unpaid.cancellation_reason, "Budget pulled.")

    def test_a_client_cannot_cancel_a_paid_project_alone(self):
        """After payment, cancelling is a financial act."""
        response = as_user(self.customer).post(
            self.url("/cancel"), {"reason": "Changed my mind."}, format="json")
        self.assertEqual(response.status_code, 403)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.IN_PROGRESS)

    def test_cancelling_a_paid_project_demands_a_refund_decision(self):
        response = as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Client vanished."}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("refund_usd", str(response.data))

    def test_zero_is_an_acceptable_decision(self):
        response = as_user(self.lead).post(
            self.url("/cancel"),
            {"reason": "All work delivered, client walked.", "refund_usd": 0},
            format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Refund.objects.count(), 0)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.CANCELLED)

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False)
    def test_cancelling_with_a_refund_raises_one(self):
        response = as_user(self.lead).post(
            self.url("/cancel"),
            {"reason": "Couldn't deliver.", "refund_usd": "400"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        refund = Refund.objects.get()
        self.assertEqual(refund.amount_usd, Decimal("400.00"))
        self.assertEqual(refund.reason, "Couldn't deliver.")

    def test_a_reason_is_always_required(self):
        self.assertEqual(
            as_user(self.lead).post(
                self.url("/cancel"), {"refund_usd": 0}, format="json").status_code, 400)

    def test_a_cancelled_project_cannot_be_cancelled_again(self):
        as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Done.", "refund_usd": 0}, format="json")
        response = as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Again.", "refund_usd": 0}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_completed_project_cannot_be_cancelled(self):
        self.project.stage = Project.Stage.COMPLETED
        self.project.save(update_fields=["stage"])
        self.assertEqual(
            as_user(self.lead).post(
                self.url("/cancel"), {"reason": "…", "refund_usd": 0},
                format="json").status_code, 400)

    def test_everyone_attached_is_told(self):
        mail.outbox = []
        as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Stopped.", "refund_usd": 0},
            format="json")
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.customer.email, recipients)
        self.assertIn(self.expert.email, recipients)
        self.assertIn(self.lead.email, recipients)

    def test_approved_work_survives_cancellation(self):
        self.approve_task(2000)
        as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Stopped.", "refund_usd": 0},
            format="json")
        self.assertEqual(
            Earning.objects.get(user=self.expert).amount_usd, Decimal("2000.00"))


class CancelledReportingTests(RefundTestBase):
    """Cancelled work must not read as in flight, or as a missed deadline."""

    def test_it_is_not_overdue(self):
        self.project.target_date = timezone.localdate() - timezone.timedelta(days=10) \
            if hasattr(timezone, "timedelta") else None
        if self.project.target_date is None:
            from datetime import timedelta as _td
            self.project.target_date = timezone.localdate() - _td(days=10)
        self.project.save(update_fields=["target_date"])
        self.assertTrue(self.project.is_overdue)

        as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Stopped.", "refund_usd": 0},
            format="json")
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_overdue)

    def test_it_stops_counting_as_live_exposure(self):
        """`in_flight_paid_usd` is the platform's "what have we already paid out
        on work that could still be refunded?" line. Cancelled work is no longer
        at risk of that, so leaving it in would overstate the exposure forever."""
        from projects import reports

        self.approve_task(2000)
        before = reports.totals()
        self.assertEqual(Decimal(before["in_flight_paid_usd"]), Decimal("2000.00"))

        as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Stopped.", "refund_usd": 0},
            format="json")

        after = reports.totals()
        self.assertEqual(Decimal(after["in_flight_paid_usd"]), Decimal("0.00"))
        self.assertEqual(Decimal(after["active_value_usd"]), Decimal("0.00"))

    def test_progress_does_not_claim_delivery(self):
        as_user(self.lead).post(
            self.url("/cancel"), {"reason": "Stopped.", "refund_usd": 0},
            format="json")
        self.project.refresh_from_db()
        self.assertLess(self.project.progress_pct, 100)
