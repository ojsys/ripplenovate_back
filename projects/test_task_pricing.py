"""Pricing tasks against the expert pool (step C).

Approving a task doesn't release money yet — that's step D. What matters here is
that a lead can only ever promise what the project holds, because once step D
lands these amounts become obligations.

The invariant under all of it: the sum of a project's task amounts never exceeds
its expert share, so the platform can't be made to pay out more than it took in.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments.models import Earning
from projects.models import Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class TaskPricingTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "pricelead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.ada = User.objects.create_user(
            "ada@ril.dev", "x", full_name="Ada Eze", role=User.Role.EXPERT)
        self.chidi = User.objects.create_user(
            "chidi@ril.dev", "x", full_name="Chidi Okonkwo", role=User.Role.EXPERT)
        for e in (self.ada, self.chidi):
            e.product_lines.add(self.line)
        # An expert who exists but was never put on this project.
        self.bench = User.objects.create_user(
            "bench@ril.dev", "x", full_name="On Bench", role=User.Role.EXPERT)
        self.bench.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "priceclient@acme.io", "x", role=User.Role.CLIENT)

        # $5,000 quote. The pool is whatever the configured expert share makes
        # it — read from the project below rather than written in here, so a
        # change to the policy moves these tests with it instead of breaking
        # them. What's under test is the allocation guard, not the percentage.
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.ada, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.ada, self.chidi)
        self.pool = self.project.expert_pool_usd

    def create(self, user=None, **payload):
        payload.setdefault("title", "A task")
        return as_user(user or self.lead).post(
            f"/api/projects/{self.project.id}/tasks", payload, format="json")

    def reassign(self, task, to=None, user=None):
        return as_user(user or self.lead).post(
            f"/api/tasks/{task.id}/reassign", {"assignee": to}, format="json")

    def edit(self, task, user=None, **payload):
        return as_user(user or self.lead).patch(
            f"/api/tasks/{task.id}", payload, format="json")

    # --- creating ---
    def test_a_lead_prices_a_task_and_assigns_it(self):
        response = self.create(title="Wireframes", assignee=self.ada.id,
                               amount_usd="800.00")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["amount_usd"], "800.00")
        self.assertEqual(response.data["status"], "todo")
        task = Task.objects.get(id=response.data["id"])
        self.assertEqual(task.assignee_id, self.ada.id)

    def test_pricing_a_task_is_recorded_in_the_feed(self):
        self.create(title="Wireframes", assignee=self.ada.id, amount_usd="800.00")
        self.assertTrue(self.project.activity.filter(
            text__contains="$800.00").exists())

    def test_tasks_can_be_split_across_the_team(self):
        half = (self.pool / 2).quantize(Decimal("0.01"))
        self.create(title="Design", assignee=self.ada.id, amount_usd=str(half))
        self.create(title="Build", assignee=self.chidi.id,
                    amount_usd=str(self.pool - half))
        self.assertEqual(self.project.allocated_usd, self.pool)
        self.assertEqual(self.project.unallocated_usd, Decimal("0.00"))
        self.assertTrue(self.project.uses_task_payouts)

    def test_an_unpriced_task_is_still_fine(self):
        """Not every task is a payment — a checklist item is still useful."""
        response = self.create(title="Kickoff call", assignee=self.ada.id)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(self.project.uses_task_payouts)

    def test_order_follows_the_existing_list(self):
        first = self.create(title="One").data
        second = self.create(title="Two").data
        self.assertEqual((first["order"], second["order"]), (0, 1))

    # --- the allocation guard ---
    def test_the_pool_can_be_allocated_exactly(self):
        """The boundary is inclusive — allocating the lot is the normal case."""
        response = self.create(title="Everything", assignee=self.ada.id,
                               amount_usd=str(self.pool))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.project.unallocated_usd, Decimal("0.00"))

    def test_one_cent_over_the_pool_is_refused(self):
        response = self.create(title="Too much", assignee=self.ada.id,
                               amount_usd=str(self.pool + Decimal("0.01")))
        self.assertEqual(response.status_code, 400)
        self.assertIn("unallocated", str(response.data))
        self.assertEqual(self.project.tasks.count(), 0)

    def test_the_guard_counts_what_is_already_allocated(self):
        first = self.pool - Decimal("500.00")
        self.create(title="First", assignee=self.ada.id, amount_usd=str(first))
        response = self.create(title="Second", assignee=self.chidi.id,
                               amount_usd="600.00")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.project.allocated_usd, first)

    def test_editing_a_task_frees_its_own_old_amount(self):
        """Otherwise re-pricing the only task down by $100 would look like
        asking for the pool plus the amount it already held."""
        task = Task.objects.get(id=self.create(
            title="All of it", assignee=self.ada.id,
            amount_usd=str(self.pool)).data["id"])
        lower = self.pool - Decimal("100.00")
        response = self.edit(task, amount_usd=str(lower))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.project.allocated_usd, lower)

    def test_editing_a_task_beyond_the_pool_is_still_refused(self):
        task = Task.objects.get(id=self.create(
            title="Some", assignee=self.ada.id, amount_usd="1000.00").data["id"])
        over = self.pool + Decimal("100.00")
        self.assertEqual(self.edit(task, amount_usd=str(over)).status_code, 400)
        task.refresh_from_db()
        self.assertEqual(task.amount_usd, Decimal("1000.00"))

    def test_a_negative_amount_is_refused(self):
        self.assertEqual(
            self.create(title="Refund?", amount_usd="-100.00").status_code, 400)

    def test_shrinking_the_expert_share_below_the_allocation_is_refused(self):
        """The same rule reached from the Django admin's direction."""
        self.create(title="Priced", assignee=self.ada.id, amount_usd="3000.00")
        self.project.refresh_from_db()
        self.project.expert_share_percent = Decimal("40")   # pool → $2,000
        with self.assertRaises(DjangoValidationError) as caught:
            self.project.full_clean()
        self.assertIn("expert_share_percent", caught.exception.error_dict)

    # --- assignment ---
    def test_a_task_cannot_go_to_someone_off_the_team(self):
        response = self.create(title="Orphan", assignee=self.bench.id,
                               amount_usd="100.00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("delivery team", str(response.data))

    def test_reassigning_to_someone_off_the_team_is_refused(self):
        task = Task.objects.get(id=self.create(title="Mine").data["id"])
        self.assertEqual(self.reassign(task, self.bench.id).status_code, 400)

    def test_a_task_can_move_between_team_members(self):
        task = Task.objects.get(id=self.create(
            title="Handover", assignee=self.ada.id, amount_usd="500.00").data["id"])
        self.assertEqual(self.reassign(task, self.chidi.id).status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.chidi.id)

    def test_the_edit_endpoint_no_longer_moves_work(self):
        """Reassignment lives at /reassign, where it can also move a task that
        has already been handed in — and where it tells both people."""
        task = Task.objects.get(id=self.create(
            title="Stays put", assignee=self.ada.id).data["id"])
        self.edit(task, assignee=self.chidi.id)
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.ada.id)

    # --- when the list is closed ---
    def test_tasks_cannot_be_priced_before_the_client_pays(self):
        self.project.stage = Project.Stage.QUOTED
        self.project.save(update_fields=["stage"])
        self.assertEqual(self.create(title="Early", amount_usd="100").status_code, 400)

    def test_a_completed_project_keeps_its_task_list_as_it_stands(self):
        self.project.stage = Project.Stage.COMPLETED
        self.project.save(update_fields=["stage"])
        self.assertEqual(self.create(title="After the fact").status_code, 400)

    def test_a_submitted_task_cannot_be_re_priced(self):
        """Changing the deal after someone has done the work."""
        task = Task.objects.get(id=self.create(
            title="Handed in", assignee=self.ada.id, amount_usd="500.00").data["id"])
        task.status = Task.Status.SUBMITTED
        task.save(update_fields=["status"])
        response = self.edit(task, amount_usd="100.00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("waiting on your review", str(response.data))

    def test_an_approved_task_is_fixed(self):
        task = Task.objects.get(id=self.create(
            title="Settled", assignee=self.ada.id, amount_usd="500.00").data["id"])
        task.status = Task.Status.APPROVED
        task.save(update_fields=["status"])
        self.assertEqual(self.edit(task, title="Rewritten").status_code, 400)

    # --- deleting ---
    def test_a_lead_deletes_an_unpaid_task_with_a_reason(self):
        task = Task.objects.get(id=self.create(title="Scrap this").data["id"])
        response = as_user(self.lead).delete(
            f"/api/tasks/{task.id}", {"reason": "Out of scope"}, format="json")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Task.objects.filter(id=task.id).exists())

    def test_a_paid_task_cannot_be_deleted(self):
        """Answers with a reason rather than a 500 from the PROTECT underneath."""
        task = Task.objects.get(id=self.create(
            title="Paid", assignee=self.ada.id, amount_usd="500.00").data["id"])
        Earning.objects.create(
            project=self.project, user=self.ada, task=task,
            kind=Earning.Kind.EXPERT, share_percent=Decimal("10.00"),
            amount_usd=Decimal("500.00"))
        response = as_user(self.lead).delete(f"/api/tasks/{task.id}")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Task.objects.filter(id=task.id).exists())

    # --- who may do any of this ---
    def test_an_expert_cannot_price_their_own_work(self):
        self.assertEqual(
            self.create(user=self.ada, title="Pay me", assignee=self.ada.id,
                        amount_usd="3000.00").status_code, 403)

    def test_the_client_cannot_touch_the_task_list(self):
        self.assertEqual(self.create(user=self.customer, title="Do this").status_code,
                         403)

    def test_status_cannot_be_set_through_the_edit_endpoint(self):
        """A writable status would be a way to pay someone with a PATCH."""
        task = Task.objects.get(id=self.create(
            title="Nice try", assignee=self.ada.id, amount_usd="500.00").data["id"])
        response = self.edit(task, status=Task.Status.APPROVED)
        self.assertEqual(response.status_code, 200)   # ignored, not rejected
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.TODO)

    # --- the payload the allocation meter reads ---
    def test_the_project_reports_its_allocation(self):
        self.create(title="Design", assignee=self.ada.id, amount_usd="1800.00")
        data = as_user(self.lead).get(f"/api/projects/{self.project.id}").data
        self.assertEqual(data["expert_pool_usd"], str(self.pool))
        self.assertEqual(data["allocated_usd"], "1800.00")
        self.assertEqual(data["unallocated_usd"],
                         str(self.pool - Decimal("1800.00")))
        self.assertTrue(data["uses_task_payouts"])


class ToggleEndpointGoneTests(TestCase):
    """The old tick-to-toggle endpoint is retired.

    It flipped a task straight to approved. Once amounts exist that would be an
    expert releasing their own money, so it goes before pricing arrives rather
    than after.
    """

    def setUp(self):
        self.expert = User.objects.create_user(
            "gone@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "goneclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A brief", client=self.customer, category="Web application",
            description="…", expert=self.expert,
            stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        self.project.experts.add(self.expert)
        self.task = Task.objects.create(project=self.project, title="A task",
                                        assignee=self.expert)

    def test_the_toggle_endpoint_no_longer_exists(self):
        response = as_user(self.expert).patch(f"/api/tasks/{self.task.id}/toggle")
        self.assertEqual(response.status_code, 404)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.TODO)
