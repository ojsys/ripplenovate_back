"""Paid extra scope on a live project (G2b).

`quote_usd` locks at payment, and it has to: the invoice the client settled and
the earnings snapshotted on approval both derive from it. But that left no
answer to "we need more than we scoped" except free work or a second brief —
so leads absorbed scope, and the revision loop had nowhere legitimate to send
genuine growth.

A change order adds to the project's *contract value* beside the quote rather
than editing it. The tests below pin the consequence that matters: every share
applies to the extra exactly as it applies to the original, so the expert pool
grows and the platform's cut doesn't quietly swallow the difference.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments.models import Earning, Payment
from projects.models import ChangeOrder, Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class ChangeOrderTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "colead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.other_lead = User.objects.create_user(
            "coother@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.other_lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "coexpert@ril.dev", "x", full_name="An Expert",
            role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "coclient@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=10000)
        self.project.experts.add(self.expert)
        self.settle(Decimal("10000"))

    def settle(self, usd, change_order=None):
        return Payment.objects.create(
            project=self.project, change_order=change_order,
            reference=f"p-{timezone.now().timestamp()}-{usd}",
            amount_subunit=int(usd * 100), currency="USD", usd_total=usd,
            status=Payment.Status.SUCCESS, paid_at=timezone.now())

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    def raise_order(self, amount="2000", by=None, description="Two extra concepts"):
        return as_user(by or self.lead).post(
            self.url("/change-orders"),
            {"amount_usd": amount, "description": description}, format="json")

    def pay_order(self, order):
        """What the Paystack webhook does, without the network.

        The payment starts PENDING on purpose — `_mark_paid` short-circuits on
        one that's already SUCCESS, so creating it settled would skip the very
        branch under test.
        """
        from payments import paystack

        payment = Payment.objects.create(
            project=self.project, change_order=order,
            reference=f"co-{timezone.now().timestamp()}-{order.id}",
            amount_subunit=int(order.amount_usd * 100), currency="USD",
            usd_total=order.amount_usd, status=Payment.Status.PENDING)
        paystack._mark_paid(payment, {})
        order.refresh_from_db()
        self.project.refresh_from_db()
        return order

    # --- raising one ---
    def test_a_lead_can_price_extra_scope(self):
        response = self.raise_order()
        self.assertEqual(response.status_code, 201, response.data)
        order = ChangeOrder.objects.get()
        self.assertEqual(order.amount_usd, Decimal("2000.00"))
        self.assertEqual(order.status, ChangeOrder.Status.AWAITING_PAYMENT)

    def test_it_changes_nothing_until_it_is_paid(self):
        self.raise_order()
        self.project.refresh_from_db()
        self.assertEqual(self.project.contract_usd, Decimal("10000.00"))
        self.assertEqual(self.project.expert_pool_usd, Decimal("7000.00"))

    def test_the_quote_is_never_touched(self):
        """The whole reason this exists rather than a re-price."""
        order = self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))
        self.project.refresh_from_db()
        self.assertEqual(self.project.quote_usd, 10000)
        self.assertEqual(self.project.contract_usd, Decimal("12000.00"))

    def test_a_description_and_an_amount_are_both_required(self):
        for payload in ({"amount_usd": "500"},
                        {"description": "Stuff"},
                        {"amount_usd": "0", "description": "Stuff"},
                        {"amount_usd": "500", "description": "   "}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    as_user(self.lead).post(
                        self.url("/change-orders"), payload,
                        format="json").status_code, 400)

    # --- who, and when ---
    def test_only_this_projects_lead_can_raise_one(self):
        for who in (self.customer, self.expert):
            with self.subTest(who=who.email):
                self.assertIn(self.raise_order(by=who).status_code, (403, 404))
        self.assertIn(self.raise_order(by=self.other_lead).status_code, (403, 404))

    def test_it_needs_a_paid_project_that_is_still_running(self):
        for stage in (Project.Stage.SUBMITTED, Project.Stage.QUOTED,
                      Project.Stage.COMPLETED, Project.Stage.CANCELLED):
            with self.subTest(stage=stage):
                self.project.stage = stage
                self.project.save(update_fields=["stage"])
                self.assertEqual(self.raise_order().status_code, 400)

    def test_it_works_across_every_live_paid_stage(self):
        for stage in (Project.Stage.PAID, Project.Stage.IN_PROGRESS,
                      Project.Stage.REVIEW):
            with self.subTest(stage=stage):
                self.project.stage = stage
                self.project.save(update_fields=["stage"])
                self.assertEqual(self.raise_order().status_code, 201)

    def test_the_client_can_see_them(self):
        self.raise_order()
        response = as_user(self.customer).get(self.url("/change-orders"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    # --- the money ---
    def test_paying_grows_the_expert_pool_by_the_expert_share(self):
        """A change order the pool doesn't grow from is just a price rise."""
        before = self.project.expert_pool_usd
        self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))
        self.assertEqual(self.project.expert_pool_usd, before + Decimal("1400.00"))

    def test_every_share_grows_in_proportion(self):
        self.project.business_developer = User.objects.create_user(
            "cobd@ril.team", "x", role=User.Role.BUSINESS_DEV)
        self.project.save(update_fields=["business_developer"])
        self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))

        split = self.project.payout_split()
        self.assertEqual(split["quote_usd"], Decimal("10000.00"))
        self.assertEqual(split["change_orders_usd"], Decimal("2000.00"))
        self.assertEqual(split["contract_usd"], Decimal("12000.00"))
        self.assertEqual(split["expert_usd"], Decimal("8400.00"))       # 70%
        self.assertEqual(split["delivery_lead_usd"], Decimal("1800.00"))  # 15%
        self.assertEqual(split["business_dev_usd"], Decimal("600.00"))    # 5%
        self.assertEqual(split["platform_usd"], Decimal("1200.00"))       # 10%

    def test_the_split_still_closes_exactly(self):
        for amount in ("1", "333.33", "2000", "9999.99"):
            with self.subTest(amount=amount):
                project = Project.objects.create(
                    title=f"P{amount}", client=self.customer,
                    category="Brand identity", description="…",
                    product_line=self.line, lead=self.lead,
                    stage=Project.Stage.IN_PROGRESS, quote_usd=7000)
                ChangeOrder.objects.create(
                    project=project, amount_usd=Decimal(amount),
                    description="More", raised_by=self.lead,
                    status=ChangeOrder.Status.PAID, paid_at=timezone.now())
                split = project.payout_split()
                total = (split["expert_usd"] + split["delivery_lead_usd"]
                         + split["business_dev_usd"] + split["platform_usd"])
                self.assertEqual(total, split["contract_usd"])

    def test_a_project_with_no_change_orders_is_completely_unchanged(self):
        """The regression that would matter most — every existing project."""
        split = self.project.payout_split()
        self.assertEqual(split["contract_usd"], split["quote_usd"])
        self.assertEqual(split["change_orders_usd"], Decimal("0.00"))
        self.assertEqual(split["expert_usd"], Decimal("7000.00"))
        self.assertEqual(self.project.expert_pool_usd, Decimal("7000.00"))

    def test_tasks_can_be_priced_against_the_new_headroom(self):
        Task.objects.create(project=self.project, title="Original",
                            assignee=self.expert, amount_usd=Decimal("7000"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.unallocated_usd, Decimal("0.00"))

        response = as_user(self.lead).post(
            self.url("/tasks"),
            {"title": "The extra work", "assignee": self.expert.id,
             "amount_usd": "1000"}, format="json")
        self.assertEqual(response.status_code, 400, "pool wasn't full")

        self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))
        response = as_user(self.lead).post(
            self.url("/tasks"),
            {"title": "The extra work", "assignee": self.expert.id,
             "amount_usd": "1000"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_over_allocation_is_still_refused_at_the_new_ceiling(self):
        self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))
        response = as_user(self.lead).post(
            self.url("/tasks"),
            {"title": "Too much", "assignee": self.expert.id,
             "amount_usd": "8401"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_task_percentages_are_measured_against_the_contract(self):
        """Measured against the quote alone they'd sum past the expert share."""
        self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))
        task = Task.objects.create(
            project=self.project, title="A chunk", assignee=self.expert,
            amount_usd=Decimal("1200"), status=Task.Status.APPROVED)

        from payments import earnings as earnings_service
        earning = earnings_service.record_task_earning(task)
        self.assertEqual(earning.share_percent, Decimal("10.00"))  # 1200/12000

    def test_the_payment_does_not_disturb_the_stage(self):
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("500"),
            description="More", raised_by=self.lead))
        self.assertEqual(self.project.stage, Project.Stage.REVIEW)

    def test_paying_twice_is_a_no_op(self):
        order = ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead)
        self.pay_order(order)
        first_paid_at = order.paid_at
        self.pay_order(order)
        order.refresh_from_db()
        self.assertEqual(order.paid_at, first_paid_at)
        self.assertEqual(self.project.contract_usd, Decimal("12000.00"))

    # --- withdrawing ---
    def test_an_unpaid_change_order_can_be_withdrawn(self):
        self.raise_order()
        order = ChangeOrder.objects.get()
        response = as_user(self.lead).delete(f"/api/change-orders/{order.id}")
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, ChangeOrder.Status.WITHDRAWN)

    def test_a_paid_one_cannot_be_withdrawn(self):
        order = self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))
        response = as_user(self.lead).delete(f"/api/change-orders/{order.id}")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Refund it instead", str(response.data))

    def test_a_withdrawn_one_adds_nothing(self):
        self.raise_order()
        order = ChangeOrder.objects.get()
        as_user(self.lead).delete(f"/api/change-orders/{order.id}")
        self.project.refresh_from_db()
        self.assertEqual(self.project.contract_usd, Decimal("10000.00"))

    # --- paying ---
    def test_only_the_client_may_pay(self):
        self.raise_order()
        order = ChangeOrder.objects.get()
        for who in (self.lead, self.expert):
            with self.subTest(who=who.email):
                self.assertEqual(
                    as_user(who).post(
                        f"/api/change-orders/{order.id}/pay/initialize"
                    ).status_code, 403)

    def test_an_already_paid_order_cannot_be_paid_again(self):
        order = self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("500"),
            description="More", raised_by=self.lead))
        self.assertEqual(
            as_user(self.customer).post(
                f"/api/change-orders/{order.id}/pay/initialize").status_code, 400)

    # --- who hears ---
    def test_raising_one_asks_the_client(self):
        mail.outbox = []
        self.raise_order()
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertEqual(recipients, {self.customer.email},
                         "only the client is asked to pay")

    def test_paying_one_tells_the_team(self):
        order = ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead)
        mail.outbox = []
        self.pay_order(order)
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.lead.email, recipients)
        self.assertIn(self.expert.email, recipients)

    # --- it interacts correctly with refunds ---
    def test_change_order_money_is_refundable(self):
        self.pay_order(ChangeOrder.objects.create(
            project=self.project, amount_usd=Decimal("2000"),
            description="More", raised_by=self.lead))
        self.assertEqual(self.project.collected_usd, Decimal("12000.00"))
        self.assertEqual(self.project.refundable_usd, Decimal("12000.00"))
