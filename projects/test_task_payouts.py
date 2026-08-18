"""Multi-expert projects and per-task payouts — the data model (step A).

Nothing here releases money yet: task approval doesn't credit an earning until
step D. What these pin is the shape underneath it, and the one promise step A
has to keep — that a project which existed before any of this pays out exactly
what it always did.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from catalog.models import ProductLine
from payments import earnings as earnings_service
from payments.models import Earning
from projects.models import Project, Task

User = get_user_model()


class TaskPayoutModelTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "poolead@ril.team", "x", full_name="A Lead", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.ada = User.objects.create_user(
            "ada@ril.dev", "x", full_name="Ada Eze", role=User.Role.EXPERT)
        self.chidi = User.objects.create_user(
            "chidi@ril.dev", "x", full_name="Chidi Okonkwo", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "poolclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.ada, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.ada, self.chidi)

    def task(self, amount="0.00", assignee=None, **kw):
        return Task.objects.create(
            project=self.project, title=kw.pop("title", "A task"),
            amount_usd=Decimal(amount), assignee=assignee or self.ada, **kw)

    # --- the pool ---
    def test_the_expert_pool_is_the_expert_share_of_the_quote(self):
        """60% of $5,000 by default."""
        self.assertEqual(self.project.expert_pool_usd, Decimal("3500.00"))

    def test_a_per_project_override_moves_the_pool(self):
        self.project.expert_share_percent = Decimal("70")
        self.assertEqual(self.project.expert_pool_usd, Decimal("3500.00"))

    def test_the_pool_reads_the_percentages_not_the_credited_rows(self):
        """`payout_split()` switches to reporting credited amounts once a project
        completes — a snapshot, so a later override can't restate history. The
        pool must not do that: allocation asks "what is this project's expert
        share?", and has to keep answering after the money has moved.

        Driven apart deliberately: credit at the standard 70%, then override to
        80%. The split still reports the $3,500 that was actually paid; the pool
        reports the $4,000 the percentages now describe. The two numbers have to
        differ or this proves nothing.
        """
        self.project.stage = Project.Stage.COMPLETED
        self.project.save(update_fields=["stage"])
        earnings_service.record_project_earnings(self.project)
        self.project.refresh_from_db()

        self.project.expert_share_percent = Decimal("80")
        self.project.save(update_fields=["expert_share_percent"])

        self.assertEqual(self.project.payout_split()["expert_usd"],
                         Decimal("3500.00"), "history was restated")
        self.assertEqual(self.project.expert_pool_usd,
                         Decimal("4000.00"), "the pool ignored the override")

    # --- allocation ---
    def test_allocation_adds_up_and_leaves_a_remainder(self):
        self.task(amount="800.00", title="Wireframes")
        self.task(amount="1200.00", title="Visual design")
        self.task(amount="600.00", title="Build", assignee=self.chidi)
        self.assertEqual(self.project.allocated_usd, Decimal("2600.00"))
        self.assertEqual(self.project.unallocated_usd, Decimal("900.00"))

    def test_an_unpriced_project_has_its_whole_pool_unallocated(self):
        self.task()
        self.task(title="Another")
        self.assertEqual(self.project.allocated_usd, Decimal("0.00"))
        self.assertEqual(self.project.unallocated_usd, self.project.expert_pool_usd)

    def test_a_negative_amount_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.task(amount="-1.00")

    # --- which payout mode a project is in ---
    def test_a_project_with_no_priced_tasks_uses_the_legacy_path(self):
        self.task()
        self.assertFalse(self.project.uses_task_payouts)

    def test_one_priced_task_switches_the_project_to_task_payouts(self):
        self.task()
        self.task(amount="0.01", title="Priced")
        self.assertTrue(self.project.uses_task_payouts)

    # --- the compatibility shim ---
    def test_done_is_a_view_of_approved(self):
        task = self.task()
        self.assertFalse(task.done)
        task.status = Task.Status.SUBMITTED
        self.assertFalse(task.done, "submitted is not done — a lead hasn't seen it")
        task.status = Task.Status.APPROVED
        self.assertTrue(task.done)

    def test_progress_still_counts_approved_tasks(self):
        self.project.stage = Project.Stage.IN_PROGRESS
        self.task(status=Task.Status.APPROVED)
        self.task(title="Two", status=Task.Status.APPROVED)
        self.task(title="Three")
        self.task(title="Four")
        self.assertEqual(self.project.progress_pct, 50)


class LegacyPayoutTests(TestCase):
    """The promise step A has to keep: an existing project pays out unchanged."""

    def setUp(self):
        self.lead = User.objects.create_user(
            "legacylead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "legacyexpert@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "legacyclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="An old brief", client=self.customer, category="Web application",
            description="…", lead=self.lead, expert=self.expert,
            stage=Project.Stage.COMPLETED, quote_usd=1000)
        self.project.experts.add(self.expert)
        # Shaped like a pre-migration project: tasks, none of them priced.
        Task.objects.create(project=self.project, title="Build it",
                            assignee=self.expert, status=Task.Status.APPROVED)

    def test_the_whole_expert_share_still_goes_to_the_primary_expert(self):
        earnings_service.record_project_earnings(self.project)
        expert_rows = Earning.objects.filter(
            project=self.project, kind=Earning.Kind.EXPERT)
        self.assertEqual(expert_rows.count(), 1)
        row = expert_rows.get()
        self.assertEqual(row.user_id, self.expert.id)
        self.assertEqual(row.amount_usd, Decimal("700.00"))   # 70% of $1,000
        self.assertIsNone(row.task_id, "a legacy row is project-level, not task-level")

    def test_the_split_still_closes_on_the_quote(self):
        earnings_service.record_project_earnings(self.project)
        total = sum(e.amount_usd for e in self.project.earnings.all())
        self.assertLessEqual(total, Decimal(self.project.quote_usd))
        split = self.project.payout_split()
        self.assertEqual(
            split["expert_usd"] + split["delivery_lead_usd"]
            + split["business_dev_usd"] + split["platform_usd"],
            Decimal("1000.00"))

    def test_crediting_twice_changes_nothing(self):
        earnings_service.record_project_earnings(self.project)
        earnings_service.record_project_earnings(self.project)
        self.assertEqual(self.project.earnings.count(), 2)  # expert + lead


class EarningShapeTests(TestCase):
    """The two constraints that replaced `unique_earning_per_role`."""

    def setUp(self):
        self.expert = User.objects.create_user(
            "shape@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "shapeclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A brief", client=self.customer, category="Web application",
            description="…", expert=self.expert,
            stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        self.task = Task.objects.create(
            project=self.project, title="A task", assignee=self.expert,
            amount_usd=Decimal("100.00"))

    def earning(self, **kw):
        return Earning.objects.create(
            project=self.project, user=self.expert, kind=Earning.Kind.EXPERT,
            share_percent=Decimal("10.00"), amount_usd=Decimal("100.00"), **kw)

    def test_a_task_cannot_be_paid_twice(self):
        self.earning(task=self.task)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.earning(task=self.task)

    def test_a_role_still_cannot_be_paid_twice_at_project_level(self):
        """The rule the old single constraint enforced, still holding."""
        self.earning()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.earning()

    def test_two_tasks_for_one_person_are_two_payments(self):
        """What the split constraint exists to allow — a NULL task can't carry
        this rule, so project-level and task-level are indexed separately."""
        second = Task.objects.create(
            project=self.project, title="Another", assignee=self.expert,
            amount_usd=Decimal("100.00"))
        self.earning(task=self.task)
        self.earning(task=second)
        self.assertEqual(Earning.objects.filter(user=self.expert).count(), 2)

    def test_a_paid_task_cannot_be_deleted(self):
        """PROTECT, not CASCADE: deleting a task must never delete a payment."""
        from django.db.models import ProtectedError

        self.earning(task=self.task)
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.task.delete()
        self.assertTrue(Earning.objects.filter(task=self.task).exists())

    def test_an_unpaid_task_can_still_be_deleted(self):
        task_id = self.task.id
        self.task.delete()
        self.assertFalse(Task.objects.filter(id=task_id).exists())
