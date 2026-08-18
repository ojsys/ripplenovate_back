"""Reporting once experts are paid per task (step F).

Task payouts land when a lead approves a task, which can be long before the
client signs the project off. That breaks an assumption the P&L was built on —
that every credited earning belongs to a project whose value is already in
`delivered_value_usd`. Counting them together made margin read far worse than
it is, and could push a young busy line negative.

Delivered costs and in-flight cash are now reported separately.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments.models import Earning
from projects import reports
from projects.models import Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class ReportingWithTaskPayoutsTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.admin = User.objects.create_superuser("repadmin@ril.team", "x")
        self.lead = User.objects.create_user(
            "replead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.ada = User.objects.create_user(
            "repada@ril.dev", "x", full_name="Ada", role=User.Role.EXPERT)
        self.ada.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "repclient@acme.io", "x", role=User.Role.CLIENT)

    def project(self, quote, stage):
        p = Project.objects.create(
            title="A brief", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.ada, stage=stage, quote_usd=quote)
        p.experts.add(self.ada)
        return p

    def paid_task(self, project, amount):
        """A task taken all the way through to approved, so it credits."""
        task = Task.objects.create(
            project=project, title="Work", assignee=self.ada,
            amount_usd=Decimal(amount))
        as_user(self.ada).post(f"/api/tasks/{task.id}/submit")
        as_user(self.lead).post(f"/api/tasks/{task.id}/approve")
        return task

    def test_in_flight_payouts_do_not_eat_the_delivered_margin(self):
        """The regression. $1,000 delivered clean, $600 released on a live
        project — margin is about the delivered work, not the two mixed."""
        self.project(1000, Project.Stage.COMPLETED)
        live = self.project(5000, Project.Stage.IN_PROGRESS)
        self.paid_task(live, "600.00")

        totals = reports.totals()
        self.assertEqual(totals["delivered_value_usd"], "1000.00")
        # 70% expert + 15% lead on the delivered project only.
        self.assertEqual(totals["paid_out_usd"], "850.00")
        self.assertEqual(totals["platform_usd"], "150.00")
        self.assertEqual(totals["margin_percent"], "15.00")

    def test_in_flight_cash_is_reported_rather_than_hidden(self):
        live = self.project(5000, Project.Stage.IN_PROGRESS)
        self.paid_task(live, "600.00")
        self.paid_task(live, "400.00")
        self.assertEqual(reports.totals()["in_flight_paid_usd"], "1000.00")

    def test_a_line_with_only_live_work_does_not_read_as_a_loss(self):
        """Before the fix this line showed a negative platform share: costs
        credited, no delivered value to set them against."""
        live = self.project(5000, Project.Stage.IN_PROGRESS)
        self.paid_task(live, "1500.00")

        row = next(r for r in reports.product_lines() if r["slug"] == self.line.slug)
        self.assertEqual(row["delivered_value_usd"], 0)
        self.assertEqual(row["expert_cost_usd"], "0.00")
        self.assertEqual(row["platform_usd"], "0.00")
        self.assertEqual(row["in_flight_paid_usd"], "1500.00")

    def test_completing_the_project_moves_the_cost_into_the_pnl(self):
        live = self.project(5000, Project.Stage.IN_PROGRESS)
        self.paid_task(live, "1500.00")
        live.stage = Project.Stage.REVIEW
        live.save(update_fields=["stage"])
        as_user(self.customer).post(f"/api/projects/{live.id}/approve")

        totals = reports.totals()
        self.assertEqual(totals["in_flight_paid_usd"], "0.00")
        self.assertEqual(totals["delivered_value_usd"], "5000.00")
        # $1,500 to the expert for the one priced task, $750 lead share.
        self.assertEqual(totals["paid_out_usd"], "2250.00")
        # The unallocated rest of the expert pool stayed with the platform.
        self.assertEqual(totals["platform_usd"], "2750.00")

    def test_reporting_credits_an_approved_task_nobody_has_looked_at(self):
        """`ensure_credited` already did this for delivered projects. An
        uncredited approved task is the same problem: a committed cost the
        report would omit, flattering the margin until its expert logs in."""
        live = self.project(5000, Project.Stage.IN_PROGRESS)
        task = self.paid_task(live, "800.00")
        Earning.objects.filter(task=task).delete()

        self.assertEqual(reports.totals()["in_flight_paid_usd"], "800.00")
        self.assertEqual(Earning.objects.filter(task=task).count(), 1)

    def test_the_ledger_still_drives_every_figure(self):
        """No re-derived percentages: change the ledger, the report follows."""
        done = self.project(1000, Project.Stage.COMPLETED)
        reports.totals()
        Earning.objects.filter(project=done, kind=Earning.Kind.DELIVERY_LEAD).delete()
        # ensure_credited only fills gaps for projects with *no* earnings at
        # all, so the remaining expert row is what the report now sees.
        self.assertEqual(reports.totals()["paid_out_usd"], "700.00")

    def test_the_reports_endpoint_carries_the_new_figure(self):
        live = self.project(5000, Project.Stage.IN_PROGRESS)
        self.paid_task(live, "600.00")
        data = as_user(self.admin).get("/api/reports").data
        self.assertEqual(data["totals"]["in_flight_paid_usd"], "600.00")
        self.assertIn("in_flight_paid_usd", data["product_lines"][0])
