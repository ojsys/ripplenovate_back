"""Approving a task releases its money (step D).

This is the step that writes money, so the tests lean hard on the boundaries:
who may approve, what happens twice, and whether the ledger can ever exceed
what the client paid.

The rule underneath all of it is the one payouts already follow one link
further down the chain — nobody settles their own withdrawal — moved earlier:
nobody approves their own task.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments import earnings as earnings_service
from payments.models import Earning
from projects.models import Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class TaskLifecycleTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "flowlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.other_lead = User.objects.create_user(
            "colead@ril.team", "x", full_name="Co Lead",
            role=User.Role.DELIVERY_LEAD)
        self.other_lead.product_lines.add(self.line)
        self.ada = User.objects.create_user(
            "ada@ril.dev", "x", full_name="Ada Eze", role=User.Role.EXPERT)
        self.chidi = User.objects.create_user(
            "chidi@ril.dev", "x", full_name="Chidi Okonkwo", role=User.Role.EXPERT)
        for e in (self.ada, self.chidi):
            e.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "flowclient@acme.io", "x", role=User.Role.CLIENT)

        # $5,000 quote → $3,000 expert pool.
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.ada, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.ada, self.chidi)
        self.task = self.make_task(self.ada, "1200.00", "Visual design")

    def make_task(self, assignee, amount, title="A task"):
        return Task.objects.create(
            project=self.project, title=title, assignee=assignee,
            amount_usd=Decimal(amount))

    def submit(self, task, user=None):
        return as_user(user or task.assignee).post(f"/api/tasks/{task.id}/submit")

    def approve(self, task, user=None):
        return as_user(user or self.lead).post(f"/api/tasks/{task.id}/approve")

    def changes(self, task, user=None, note="Needs more contrast"):
        return as_user(user or self.lead).post(
            f"/api/tasks/{task.id}/request-changes", {"note": note}, format="json")

    def balance(self, user):
        return earnings_service.summary(user)

    # --- the happy path ---
    def test_submit_then_approve_releases_the_money(self):
        self.assertEqual(self.submit(self.task).status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.SUBMITTED)
        self.assertIsNotNone(self.task.submitted_at)
        self.assertEqual(self.balance(self.ada)["available_usd"], Decimal("0.00"))

        self.assertEqual(self.approve(self.task).status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.APPROVED)
        self.assertEqual(self.task.approved_by_id, self.lead.id)
        self.assertEqual(self.balance(self.ada)["available_usd"], Decimal("1200.00"))

    def test_the_earning_points_at_the_task_that_paid_it(self):
        self.submit(self.task)
        self.approve(self.task)
        earning = Earning.objects.get(task=self.task)
        self.assertEqual(earning.user_id, self.ada.id)
        self.assertEqual(earning.kind, Earning.Kind.EXPERT)
        self.assertEqual(earning.amount_usd, Decimal("1200.00"))
        # 1200 of a 5000 quote.
        self.assertEqual(earning.share_percent, Decimal("24.00"))

    def test_the_money_is_withdrawable_before_the_client_signs_off(self):
        """The whole point of the feature: the client paid up front, so an
        expert isn't waiting on project completion to be paid for finished work."""
        self.submit(self.task)
        self.approve(self.task)
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.stage, Project.Stage.COMPLETED)
        self.assertEqual(self.balance(self.ada)["available_usd"], Decimal("1200.00"))

    def test_changes_send_it_back_and_nothing_is_paid(self):
        self.submit(self.task)
        self.assertEqual(self.changes(self.task).status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.CHANGES)
        self.assertIsNone(self.task.submitted_at)
        self.assertFalse(Earning.objects.filter(task=self.task).exists())
        # And it can go round again.
        self.assertEqual(self.submit(self.task).status_code, 200)

    def test_a_reason_is_required_to_send_a_task_back(self):
        self.submit(self.task)
        response = as_user(self.lead).post(
            f"/api/tasks/{self.task.id}/request-changes", {"note": "  "},
            format="json")
        self.assertEqual(response.status_code, 400)

    # --- who may do what ---
    def test_a_task_cannot_be_assigned_to_a_delivery_lead(self):
        """The first line of defence, and the one that does the real work: a
        task goes to an expert. A lead can't be on the delivery team, so they
        can't hold a task, so they can never be their own approver."""
        response = as_user(self.lead).post(
            f"/api/projects/{self.project.id}/tasks",
            {"title": "Mine", "assignee": self.lead.id, "amount_usd": "300.00"},
            format="json")
        self.assertEqual(response.status_code, 400)
        response = as_user(self.lead).post(
            f"/api/projects/{self.project.id}/experts",
            {"experts": [self.other_lead.id]}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_nobody_approves_their_own_task(self):
        """Defence in depth. The API can't produce this state — the check above
        stops it — so the state is built directly here to prove the guard on the
        money itself holds if anything ever does reach it.

        An admin is the way out: peer leads can't see another lead's project at
        all, which is the scoping rule working, not a gap.
        """
        self.project.experts.add(self.lead)
        own = self.make_task(self.lead, "300.00", "Lead's own task")
        self.submit(own, user=self.lead)
        response = self.approve(own, user=self.lead)
        self.assertEqual(response.status_code, 403)
        self.assertIn("your own task", str(response.data))
        self.assertFalse(Earning.objects.filter(task=own).exists())

        admin = User.objects.create_superuser("taskadmin@ril.team", "x")
        self.assertEqual(self.approve(own, user=admin).status_code, 200)
        self.assertEqual(Earning.objects.filter(task=own).count(), 1)

    def test_a_peer_lead_cannot_approve_on_someone_elses_project(self):
        """Not a gap — the same scoping that keeps one lead out of another
        lead's brief entirely."""
        self.submit(self.task)
        self.assertEqual(self.approve(self.task, user=self.other_lead).status_code, 403)

    def test_an_expert_cannot_approve_anything(self):
        self.submit(self.task)
        for someone in (self.ada, self.chidi):
            self.assertEqual(self.approve(self.task, user=someone).status_code, 403)
        self.assertFalse(Earning.objects.filter(task=self.task).exists())

    def test_only_the_assignee_hands_a_task_in(self):
        """Not the lead 'on their behalf' — that would be one person doing the
        whole chain, and approval is what releases the money."""
        for someone in (self.lead, self.chidi):
            self.assertEqual(self.submit(self.task, user=someone).status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.TODO)

    def test_a_lead_from_another_project_cannot_approve(self):
        stranger = User.objects.create_user(
            "stranger@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.submit(self.task)
        self.assertEqual(self.approve(self.task, user=stranger).status_code, 403)

    def test_a_lead_still_in_review_cannot_approve(self):
        pending = User.objects.create_user(
            "pendingapprover@ril.team", "x", role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING)
        pending.product_lines.add(self.line)
        self.submit(self.task)
        self.assertEqual(self.approve(self.task, user=pending).status_code, 403)

    # --- the state machine ---
    def test_a_task_must_be_handed_in_before_it_can_be_approved(self):
        response = self.approve(self.task)
        self.assertEqual(response.status_code, 400)
        self.assertIn("hasn't been handed in", str(response.data))
        self.assertFalse(Earning.objects.filter(task=self.task).exists())

    def test_approving_twice_pays_once(self):
        self.submit(self.task)
        self.approve(self.task)
        second = self.approve(self.task)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(Earning.objects.filter(task=self.task).count(), 1)
        self.assertEqual(self.balance(self.ada)["available_usd"], Decimal("1200.00"))

    def test_an_approved_task_cannot_be_sent_back(self):
        self.submit(self.task)
        self.approve(self.task)
        self.assertEqual(self.changes(self.task).status_code, 400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.APPROVED)

    def test_an_approved_task_cannot_be_resubmitted(self):
        self.submit(self.task)
        self.approve(self.task)
        self.assertEqual(self.submit(self.task).status_code, 400)

    def test_an_unpriced_task_approves_without_paying_anything(self):
        free = Task.objects.create(project=self.project, title="Kickoff call",
                                   assignee=self.ada)
        self.submit(free)
        self.assertEqual(self.approve(free).status_code, 200)
        self.assertFalse(Earning.objects.filter(task=free).exists())

    # --- money, in aggregate ---
    def test_two_experts_each_draw_only_their_own_tasks(self):
        chidi_task = self.make_task(self.chidi, "800.00", "Build")
        for task in (self.task, chidi_task):
            self.submit(task)
            self.approve(task)
        self.assertEqual(self.balance(self.ada)["available_usd"], Decimal("1200.00"))
        self.assertEqual(self.balance(self.chidi)["available_usd"], Decimal("800.00"))

    def test_pending_shows_an_expert_their_own_outstanding_tasks(self):
        """Not the project's whole expert share — on a team that would show
        each of them the same money."""
        self.make_task(self.chidi, "800.00", "Build")
        self.assertEqual(self.balance(self.ada)["pending_usd"], Decimal("1200.00"))
        self.assertEqual(self.balance(self.chidi)["pending_usd"], Decimal("800.00"))

    def test_approved_work_moves_from_pending_to_available(self):
        self.submit(self.task)
        self.approve(self.task)
        summary = self.balance(self.ada)
        self.assertEqual(summary["available_usd"], Decimal("1200.00"))
        self.assertEqual(summary["pending_usd"], Decimal("0.00"))

    def test_the_ledger_never_exceeds_the_quote(self):
        """The invariant the whole feature rests on."""
        self.task.amount_usd = Decimal("1800.00")
        self.task.save(update_fields=["amount_usd"])
        chidi_task = self.make_task(self.chidi, "1200.00", "Build")
        for task in (self.task, chidi_task):
            self.submit(task)
            self.approve(task)
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        as_user(self.customer).post(f"/api/projects/{self.project.id}/approve")

        total = sum(e.amount_usd for e in self.project.earnings.all())
        self.assertLessEqual(total, Decimal(self.project.quote_usd))
        # Expert pool exactly consumed; lead takes 15%, no BD on this one.
        self.assertEqual(total, Decimal("3750.00"))

    def test_completion_does_not_pay_the_experts_a_second_time(self):
        self.submit(self.task)
        self.approve(self.task)
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        as_user(self.customer).post(f"/api/projects/{self.project.id}/approve")

        expert_rows = Earning.objects.filter(
            project=self.project, kind=Earning.Kind.EXPERT)
        self.assertEqual(expert_rows.count(), 1)
        self.assertEqual(expert_rows.get().task_id, self.task.id)
        self.assertEqual(self.balance(self.ada)["available_usd"], Decimal("1200.00"))

    def test_unallocated_pool_stays_with_the_platform(self):
        """$1,200 of a $3,000 pool was priced; the rest isn't anyone's."""
        self.submit(self.task)
        self.approve(self.task)
        self.project.stage = Project.Stage.COMPLETED
        self.project.save(update_fields=["stage"])
        earnings_service.record_project_earnings(self.project)
        self.project.refresh_from_db()
        split = self.project.payout_split()
        self.assertEqual(split["expert_usd"], Decimal("1200.00"))
        self.assertEqual(
            split["expert_usd"] + split["delivery_lead_usd"]
            + split["business_dev_usd"] + split["platform_usd"],
            Decimal("5000.00"))

    def test_the_split_reports_the_whole_expert_share_not_one_task(self):
        """`payout_split` used to take the percentage from whichever earning row
        came first, which with task rows would report one task as the lot."""
        chidi_task = self.make_task(self.chidi, "800.00", "Build")
        for task in (self.task, chidi_task):
            self.submit(task)
            self.approve(task)
        self.project.stage = Project.Stage.COMPLETED
        self.project.save(update_fields=["stage"])
        self.project.refresh_from_db()
        split = self.project.payout_split()
        self.assertEqual(split["expert_usd"], Decimal("2000.00"))
        self.assertEqual(split["expert_percent"], Decimal("40.00"))

    # --- self-healing ---
    def test_the_read_path_repairs_an_uncredited_approved_task(self):
        """Same property the project ledger already had: if the write path
        missed one, opening the earnings screen puts it right."""
        self.submit(self.task)
        self.approve(self.task)
        Earning.objects.filter(task=self.task).delete()
        self.assertEqual(self.balance(self.ada)["available_usd"], Decimal("1200.00"))
        self.assertEqual(Earning.objects.filter(task=self.task).count(), 1)

    def test_backfill_does_not_double_credit(self):
        self.submit(self.task)
        self.approve(self.task)
        for _ in range(3):
            earnings_service.backfill(self.ada)
        self.assertEqual(Earning.objects.filter(task=self.task).count(), 1)

    # --- notifications ---
    def test_the_lead_hears_about_a_submission_and_the_expert_about_the_money(self):
        mail.outbox = []
        self.submit(self.task)
        self.assertIn(self.lead.email, {a for m in mail.outbox for a in m.to})

        mail.outbox = []
        self.approve(self.task)
        by_recipient = {a: m.subject for m in mail.outbox for a in m.to}
        self.assertIn("$1,200.00", by_recipient.get(self.ada.email, ""))
        # The client is told the work landed, never what it cost to deliver.
        client_mail = next(m for m in mail.outbox if self.customer.email in m.to)
        self.assertNotIn("1,200", client_mail.body)


class LegacyProjectPayoutTests(TestCase):
    """A project with no priced tasks still pays the old way, whole."""

    def setUp(self):
        self.lead = User.objects.create_user(
            "oldlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "oldexpert@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "oldclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="An old brief", client=self.customer, category="Web application",
            description="…", lead=self.lead, expert=self.expert,
            stage=Project.Stage.REVIEW, quote_usd=1000)
        self.project.experts.add(self.expert)
        Task.objects.create(project=self.project, title="Build it",
                            assignee=self.expert, status=Task.Status.APPROVED)

    def test_the_expert_is_paid_the_whole_share_on_completion(self):
        as_user(self.customer).post(f"/api/projects/{self.project.id}/approve")
        rows = Earning.objects.filter(project=self.project,
                                      kind=Earning.Kind.EXPERT)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().amount_usd, Decimal("600.00"))
        self.assertIsNone(rows.get().task_id)

    def test_an_approved_unpriced_task_credits_nothing_on_its_own(self):
        self.assertEqual(
            earnings_service.summary(self.expert)["available_usd"], Decimal("0.00"))

    def test_pending_still_projects_the_whole_share(self):
        self.assertEqual(
            earnings_service.summary(self.expert)["pending_usd"], Decimal("600.00"))
