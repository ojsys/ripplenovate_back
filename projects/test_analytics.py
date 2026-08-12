"""The platform's own books.

Two things have to hold. The gate: this is the business's finances, not a
delivery lead's report, and the delivery_lead role can't be the check because
admins carry it. The arithmetic: the platform's share is a remainder, so the
parts always close on the whole exactly — and accrual figures never get mixed
with cash ones.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments.models import Earning, Payment, Withdrawal
from projects import analytics
from projects.models import Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class AnalyticsAccessTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("anadmin@ril.team", "x")
        self.staff = User.objects.create_user(
            "anstaff@ril.team", "x", role=User.Role.DELIVERY_LEAD, is_staff=True)
        self.lead = User.objects.create_user(
            "anlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "anexpert@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "anclient@acme.io", "x", role=User.Role.CLIENT)

    def test_an_admin_sees_it(self):
        self.assertEqual(as_user(self.admin).get("/api/analytics").status_code, 200)

    def test_staff_see_it(self):
        """The ask was admins *and* staff."""
        self.assertEqual(as_user(self.staff).get("/api/analytics").status_code, 200)

    def test_an_ordinary_delivery_lead_does_not(self):
        """The trap this codebase keeps setting: an admin has the lead role, so
        gating on the role would open the books to every lead."""
        self.assertEqual(as_user(self.lead).get("/api/analytics").status_code, 403)

    def test_experts_and_clients_do_not(self):
        for someone in (self.expert, self.customer):
            self.assertEqual(
                as_user(someone).get("/api/analytics").status_code, 403, someone.email)

    def test_the_user_payload_carries_the_staff_flag(self):
        """The nav has no other way to tell — both wear the same role."""
        self.assertTrue(as_user(self.staff).get("/api/auth/me").data["is_staff"])
        self.assertFalse(as_user(self.lead).get("/api/auth/me").data["is_staff"])


class PlatformEarningsTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.admin = User.objects.create_superuser("moneyadmin@ril.team", "x")
        self.lead = User.objects.create_user(
            "moneylead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "moneyexpert@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "moneyclient@acme.io", "x", role=User.Role.CLIENT, company="Acme")

    def delivered(self, quote=1000, **kw):
        p = Project.objects.create(
            title="A brief", client=self.customer, company="Acme",
            category="Brand identity", description="…", product_line=self.line,
            lead=self.lead, expert=self.expert, stage=Project.Stage.COMPLETED,
            completed_at=timezone.now(), quote_usd=quote, **kw)
        p.experts.add(self.expert)
        return p

    def test_the_platform_keeps_what_nobody_else_claimed(self):
        """$1,000 quote: 60% expert, 15% lead, no BD — the platform's 25%."""
        project = self.delivered(1000)
        from payments import earnings as earnings_service
        earnings_service.record_project_earnings(project)

        data = analytics.platform_earnings()
        self.assertEqual(data["delivered_value_usd"], "1000.00")
        self.assertEqual(data["team_cost_usd"], "750.00")
        self.assertEqual(data["earned_usd"], "250.00")
        self.assertEqual(data["margin_percent"], "25.00")

    def test_the_parts_always_close_on_the_whole(self):
        """The invariant. The platform's share is a remainder, never a
        re-derived percentage, so rounding can't open a gap."""
        for quote in (999, 1000, 3333, 7777):
            self.delivered(quote)
        analytics.dashboard()   # credits anything uncredited

        split = analytics.where_it_goes()
        parts = sum(Decimal(p["amount_usd"]) for p in split["parts"])
        self.assertEqual(parts, Decimal(split["total_usd"]))

    def test_it_reads_the_ledger_not_the_percentages(self):
        """A project paid on an override is counted as it was actually paid."""
        project = self.delivered(1000)
        Earning.objects.create(
            project=project, user=self.expert, kind=Earning.Kind.EXPERT,
            share_percent=Decimal("80.00"), amount_usd=Decimal("800.00"))
        self.assertEqual(analytics.platform_earnings()["earned_usd"], "200.00")

    def test_task_payouts_are_counted_as_team_cost(self):
        """An expert paid task by task costs the platform the same as one paid
        in a lump at completion."""
        project = self.delivered(1000)
        task = Task.objects.create(
            project=project, title="Work", assignee=self.expert,
            amount_usd=Decimal("400.00"), status=Task.Status.APPROVED)
        from payments import earnings as earnings_service
        earnings_service.record_task_earning(task)
        earnings_service.record_project_earnings(project)

        data = analytics.platform_earnings()
        # 400 to the expert by task + 150 lead share; no whole-pool expert row.
        self.assertEqual(data["team_cost_usd"], "550.00")
        self.assertEqual(data["earned_usd"], "450.00")

    def test_unfinished_work_is_not_counted_as_earned(self):
        Project.objects.create(
            title="Still going", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.assertEqual(analytics.platform_earnings()["earned_usd"], "0.00")

    def test_a_month_with_no_deliveries_has_no_margin_rather_than_zero(self):
        """None, not 0% — a month that hasn't happened isn't a bad month."""
        self.assertIsNone(analytics.platform_earnings()["margin_percent"])
        self.assertIsNone(analytics.platform_earnings()["change_percent"])

    # --- cash, kept apart from accrual ---
    def test_cash_and_earnings_are_different_questions(self):
        project = self.delivered(1000)
        from payments import earnings as earnings_service
        earnings_service.record_project_earnings(project)
        Payment.objects.create(
            project=project, reference="ref-1", amount_subunit=101500,
            usd_total=Decimal("1015.00"), status=Payment.Status.SUCCESS)

        cash = analytics.cash_position()
        # What the client actually paid, processing fee included — not the quote.
        self.assertEqual(cash["collected_usd"], "1015.00")
        self.assertEqual(cash["credited_usd"], "750.00")
        self.assertEqual(cash["paid_out_usd"], "0.00")
        # Credited but not drawn: a liability the platform is holding.
        self.assertEqual(cash["owed_to_team_usd"], "750.00")

    def test_a_requested_withdrawal_counts_against_what_is_owed(self):
        project = self.delivered(1000)
        from payments import earnings as earnings_service
        earnings_service.record_project_earnings(project)
        Withdrawal.objects.create(
            user=self.expert, reference="wd-1", amount_usd=Decimal("100.00"),
            bank_name="B", bank_account_number="1", bank_account_name="X",
            status=Withdrawal.Status.REQUESTED)
        cash = analytics.cash_position()
        self.assertEqual(cash["pending_withdrawals_usd"], "100.00")
        self.assertEqual(cash["owed_to_team_usd"], "650.00")

    def test_exposure_is_money_out_on_unfinished_work(self):
        live = Project.objects.create(
            title="Live", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        live.experts.add(self.expert)
        task = Task.objects.create(
            project=live, title="Work", assignee=self.expert,
            amount_usd=Decimal("600.00"), status=Task.Status.APPROVED)
        from payments import earnings as earnings_service
        earnings_service.record_task_earning(task)
        self.assertEqual(analytics.cash_position()["exposure_usd"], "600.00")

    # --- shape of the payload the dashboard reads ---
    def test_the_trend_covers_twelve_months_including_empty_ones(self):
        """A chart with gaps in the axis lies about the shape of the year."""
        months = analytics.monthly_trend()
        self.assertEqual(len(months), 12)
        self.assertEqual(months[-1]["month"],
                         f"{timezone.localdate():%Y-%m}")

    def test_a_delivered_project_lands_in_its_completion_month(self):
        self.delivered(1000)
        analytics.dashboard()
        this_month = analytics.monthly_trend()[-1]
        self.assertEqual(this_month["delivered_usd"], "1000.00")
        self.assertEqual(this_month["delivered_count"], 1)

    def test_the_dashboard_credits_anything_the_write_path_missed(self):
        """Reporting can't inherit the lazy backfill: an uncredited project has
        income and no cost, so it reads as pure platform margin."""
        self.delivered(1000)
        self.assertEqual(Earning.objects.count(), 0)
        data = analytics.dashboard()
        self.assertEqual(data["platform"]["earned_usd"], "250.00")

    def test_the_endpoint_returns_every_section(self):
        self.delivered(1000)
        data = as_user(self.admin).get("/api/analytics").data
        for key in ("platform", "cash", "monthly", "split",
                    "product_lines", "pipeline", "top_clients"):
            self.assertIn(key, data)
        self.assertEqual(data["top_clients"][0]["company"], "Acme")
