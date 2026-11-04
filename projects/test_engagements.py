"""Retainers billed monthly (G5).

Virtual assistance, bookkeeping and social media management are seats, not
projects — the same work every month for as long as somebody wants it. The
platform launched all of them as product lines while the money model could only
express a one-shot brief.

**The design under test: a cycle is a Project.** Each month materialises an
ordinary project pointing at its engagement, so the lifecycle, the task payouts,
the earnings ledger and the refund machinery all work untouched. The most
important test in this file is the one proving a cycle's payout is identical to
a standalone project's — if that ever stops holding, the design has quietly
grown a second money path.

Generation is the only job here that creates billable records with nobody
watching, so most of the rest is about it refusing to do the wrong thing.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Organisation, OrganisationMember
from catalog.models import ProductLine
from payments.models import Earning
from projects import engagements as service
from projects.models import CycleRun, Engagement, Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class EngagementTestBase(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="virtual-operations")
        self.lead = User.objects.create_user(
            "enlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.other_lead = User.objects.create_user(
            "enother@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.va = User.objects.create_user(
            "enva@ril.dev", "x", full_name="A VA",
            role=User.Role.EXPERT, lead=self.lead)
        self.va.product_lines.add(self.line)
        self.org = Organisation.objects.create(name="Acme Ltd", slug="acme-en")
        self.buyer = User.objects.create_user(
            "enbuyer@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=self.org, user=self.buyer, role="owner")
        self.admin = User.objects.create_superuser("enboss@ril.team", "x")

    def engagement(self, *, started=None, billing_day=1, amount="1200",
                   status=Engagement.Status.ACTIVE, ends=None):
        return Engagement.objects.create(
            organisation=self.org, client=self.buyer, lead=self.lead,
            product_line=self.line, title="Virtual assistant",
            description="Inbox, calendar and travel.",
            monthly_amount_usd=Decimal(amount), billing_day=billing_day,
            status=status, started_on=started or date(2026, 1, 1), ends_on=ends,
        )


class CycleGenerationTests(EngagementTestBase):
    def test_a_cycle_is_an_ordinary_project(self):
        e = self.engagement()
        cycle = service.generate_cycle(e, date(2026, 3, 1))

        self.assertIsInstance(cycle, Project)
        self.assertEqual(cycle.engagement_id, e.id)
        self.assertEqual(cycle.period_start, date(2026, 3, 1))
        self.assertEqual(cycle.period_end, date(2026, 3, 31))
        self.assertEqual(cycle.quote_usd, 1200)
        self.assertEqual(cycle.stage, Project.Stage.QUOTED)
        self.assertEqual(cycle.client_id, self.buyer.id)
        self.assertEqual(cycle.organisation_id, self.org.id)
        self.assertEqual(cycle.lead_id, self.lead.id)

    def test_it_goes_straight_to_quoted(self):
        """The price was agreed when the retainer was set up. There is nothing
        to quote each month — only to pay."""
        cycle = service.generate_cycle(self.engagement(), date(2026, 3, 1))
        self.assertEqual(cycle.stage, Project.Stage.QUOTED)

    def test_the_period_runs_to_the_day_before_the_next_one(self):
        e = self.engagement(billing_day=15)
        cycle = service.generate_cycle(e, date(2026, 3, 15))
        self.assertEqual(cycle.period_end, date(2026, 4, 14))

    def test_february_is_handled(self):
        e = self.engagement(billing_day=28)
        cycle = service.generate_cycle(e, date(2026, 1, 28))
        self.assertEqual(cycle.period_end, date(2026, 2, 27))

    def test_generating_the_same_period_twice_is_a_no_op(self):
        """The guard that stops a double run double-billing a real client."""
        e = self.engagement()
        first = service.generate_cycle(e, date(2026, 3, 1))
        second = service.generate_cycle(e, date(2026, 3, 1))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(e.cycles.count(), 1)

    def test_the_team_carries_forward(self):
        """A lead staffs the first month and doesn't re-pick the same people
        every month afterwards."""
        e = self.engagement()
        first = service.generate_cycle(e, date(2026, 3, 1))
        first.experts.add(self.va)
        first.expert = self.va
        first.save(update_fields=["expert"])

        second = service.generate_cycle(e, date(2026, 4, 1))
        self.assertEqual([x.id for x in second.experts.all()], [self.va.id])
        self.assertEqual(second.expert_id, self.va.id)

    def test_the_first_cycle_has_nobody_on_it_yet(self):
        cycle = service.generate_cycle(self.engagement(), date(2026, 3, 1))
        self.assertEqual(cycle.experts.count(), 0)
        self.assertIsNone(cycle.expert_id)


class PeriodMathTests(EngagementTestBase):
    def test_the_first_period_starts_on_the_billing_day(self):
        e = self.engagement(started=date(2026, 3, 1), billing_day=1)
        self.assertEqual(service.next_period_start(e), date(2026, 3, 1))

    def test_a_billing_day_already_past_rolls_to_next_month(self):
        """Half a month is not a month, and billing for one would be wrong."""
        e = self.engagement(started=date(2026, 3, 20), billing_day=1)
        self.assertEqual(service.next_period_start(e), date(2026, 4, 1))

    def test_it_follows_the_cycles_that_exist(self):
        e = self.engagement()
        service.generate_cycle(e, date(2026, 3, 1))
        self.assertEqual(service.next_period_start(e), date(2026, 4, 1))

    def test_december_rolls_into_january(self):
        e = self.engagement()
        service.generate_cycle(e, date(2026, 12, 1))
        self.assertEqual(service.next_period_start(e), date(2027, 1, 1))


class GenerationGuardTests(EngagementTestBase):
    def test_a_paused_retainer_is_skipped(self):
        e = self.engagement(status=Engagement.Status.PAUSED)
        due, why = service.due_for_generation(e, date(2026, 3, 1))
        self.assertFalse(due)
        self.assertIn("paused", why)

    def test_an_ended_retainer_is_skipped(self):
        e = self.engagement(status=Engagement.Status.ENDED)
        self.assertFalse(service.due_for_generation(e, date(2026, 3, 1))[0])

    def test_an_unpaid_cycle_blocks_the_next(self):
        """The credit control, and the reason net-30 was deferred: a client who
        hasn't paid for June doesn't get July as well."""
        e = self.engagement()
        service.generate_cycle(e, date(2026, 3, 1))
        due, why = service.due_for_generation(e, date(2026, 3, 28))
        self.assertFalse(due)
        self.assertIn("unpaid", why)

    def test_a_paid_cycle_does_not_block(self):
        e = self.engagement()
        cycle = service.generate_cycle(e, date(2026, 3, 1))
        cycle.stage = Project.Stage.IN_PROGRESS
        cycle.save(update_fields=["stage"])
        self.assertTrue(service.due_for_generation(e, date(2026, 3, 28))[0])

    def test_a_cycle_raised_early_and_not_yet_due_does_not_block(self):
        """Unpaid isn't the same as overdue — one raised a week ahead is fine.

        Dated off the real clock rather than a literal: "has this period
        started?" is answered against today, so a hardcoded date would drift
        into the past and the test would stop meaning anything.
        """
        e = self.engagement()
        future = timezone.localdate() + timedelta(days=20)
        service.generate_cycle(e, future)
        self.assertIsNone(service.blocking_cycle(e))

    def test_nothing_is_raised_more_than_a_week_ahead(self):
        e = self.engagement(started=date(2026, 3, 1))
        due, why = service.due_for_generation(e, date(2026, 2, 1))
        self.assertFalse(due)
        self.assertIn("not due", why)

    def test_it_is_raised_a_week_before_the_period(self):
        e = self.engagement(started=date(2026, 3, 1))
        self.assertTrue(service.due_for_generation(e, date(2026, 2, 22))[0])

    def test_an_end_date_stops_generation(self):
        e = self.engagement(ends=date(2026, 3, 15))
        service.generate_cycle(e, date(2026, 3, 1))
        due, why = service.due_for_generation(e, date(2026, 3, 26))
        self.assertFalse(due)


class ScheduledRunTests(EngagementTestBase):
    def test_a_run_is_logged_even_when_it_does_nothing(self):
        """"Did the cron fire?" is a question somebody asks at 2am."""
        entry, created = service.run(on=date(2026, 2, 1))
        self.assertEqual(created, [])
        self.assertEqual(CycleRun.objects.count(), 1)
        self.assertEqual(entry.created_count, 0)

    def test_a_dry_run_creates_nothing(self):
        self.engagement(started=date(2026, 3, 1))
        entry, created = service.run(dry_run=True, on=date(2026, 2, 25))
        self.assertEqual(len(created), 1)
        self.assertEqual(Project.objects.count(), 0)
        self.assertTrue(entry.dry_run)

    def test_a_real_run_raises_and_notifies(self):
        self.engagement(started=date(2026, 3, 1))
        mail.outbox = []
        entry, created = service.run(on=date(2026, 2, 25))
        self.assertEqual(len(created), 1)
        self.assertEqual(Project.objects.count(), 1)
        to = {addr for m in mail.outbox for addr in m.to}
        self.assertEqual(to, {self.buyer.email},
                         "only the client — there's no work to do until it's paid")

    def test_the_cap_stops_a_runaway(self):
        """A run wanting more than the cap has almost certainly been handed a
        bad date. Stopping beats billing forty clients twice."""
        for n in range(4):
            Engagement.objects.create(
                organisation=self.org, client=self.buyer, lead=self.lead,
                product_line=self.line, title=f"Seat {n}", description="…",
                monthly_amount_usd=Decimal("100"), billing_day=1,
                started_on=date(2026, 3, 1))
        entry, created = service.run(on=date(2026, 2, 25), limit=2)
        self.assertEqual(len(created), 2)
        self.assertIn("cap", entry.detail)

    def test_running_twice_in_a_day_bills_once(self):
        self.engagement(started=date(2026, 3, 1))
        service.run(on=date(2026, 2, 25))
        service.run(on=date(2026, 2, 25))
        self.assertEqual(Project.objects.count(), 1)

    def test_the_command_runs(self):
        self.engagement(started=date(2026, 3, 1))
        call_command("roll_cycles", "--dry-run", "--on=2026-02-25")
        self.assertEqual(Project.objects.count(), 0)
        call_command("roll_cycles", "--on=2026-02-25")
        self.assertEqual(Project.objects.count(), 1)


class CyclePayoutTests(EngagementTestBase):
    """The payoff from "a cycle is a project": no new payout arithmetic."""

    def _complete(self, project):
        project.experts.add(self.va)
        project.expert = self.va
        project.stage = Project.Stage.REVIEW
        project.save(update_fields=["expert", "stage"])
        as_user(self.buyer).post(f"/api/projects/{project.id}/approve")
        project.refresh_from_db()
        return project

    def test_a_cycle_pays_out_exactly_like_a_standalone_project(self):
        """If this ever fails, the design has grown a second money path."""
        e = self.engagement(amount="1000")
        cycle = self._complete(service.generate_cycle(e, date(2026, 3, 1)))

        standalone = Project.objects.create(
            title="A one-off", client=self.buyer, organisation=self.org,
            category="Virtual assistance", description="…",
            product_line=self.line, lead=self.lead,
            stage=Project.Stage.REVIEW, quote_usd=1000)
        standalone = self._complete(standalone)

        def split(p):
            s = p.payout_split()
            return (s["expert_usd"], s["delivery_lead_usd"],
                    s["business_dev_usd"], s["platform_usd"])

        self.assertEqual(split(cycle), split(standalone))

    def test_task_payouts_work_on_a_cycle(self):
        e = self.engagement(amount="1000")
        cycle = service.generate_cycle(e, date(2026, 3, 1))
        cycle.stage = Project.Stage.IN_PROGRESS
        cycle.experts.add(self.va)
        cycle.expert = self.va
        cycle.save(update_fields=["stage", "expert"])

        self.assertEqual(cycle.expert_pool_usd, Decimal("700.00"))
        task = Task.objects.create(
            project=cycle, title="March inbox", assignee=self.va,
            amount_usd=Decimal("700"), status=Task.Status.APPROVED)
        from payments import earnings as earnings_service
        earning = earnings_service.record_task_earning(task)
        self.assertEqual(earning.amount_usd, Decimal("700.00"))

    def test_each_month_is_paid_separately(self):
        e = self.engagement(amount="1000")
        for period in (date(2026, 3, 1), date(2026, 4, 1)):
            self._complete(service.generate_cycle(e, period))
        rows = Earning.objects.filter(
            user=self.va, kind=Earning.Kind.EXPERT)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(sum(r.amount_usd for r in rows), Decimal("1400.00"))

    def test_a_cycle_can_be_refunded_like_any_project(self):
        from payments.models import Payment, Refund

        e = self.engagement(amount="1000")
        cycle = service.generate_cycle(e, date(2026, 3, 1))
        Payment.objects.create(
            project=cycle, reference="cyc-1", amount_subunit=100000,
            currency="USD", usd_total=Decimal("1000"),
            status=Payment.Status.SUCCESS, paid_at=timezone.now())
        cycle.stage = Project.Stage.IN_PROGRESS
        cycle.save(update_fields=["stage"])

        with self.settings(PAYSTACK_REFUNDS_ENABLED=False):
            response = as_user(self.admin).post(
                f"/api/projects/{cycle.id}/refunds",
                {"amount_usd": "250", "reason": "Half a month unworked."},
                format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Refund.objects.get().amount_usd, Decimal("250.00"))


class EngagementApiTests(EngagementTestBase):
    def create(self, by=None, **overrides):
        payload = {
            "title": "Virtual assistant", "description": "Inbox and calendar.",
            "monthly_amount_usd": "1200", "billing_day": 1,
            "started_on": "2026-03-01", "product_line": self.line.slug,
            "client": self.buyer.id,
        }
        payload.update(overrides)
        return as_user(by or self.lead).post(
            "/api/engagements", payload, format="json")

    def test_a_lead_sets_one_up(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        e = Engagement.objects.get()
        self.assertEqual(e.lead_id, self.lead.id)
        self.assertEqual(e.organisation_id, self.org.id)

    def test_a_client_cannot_set_one_up(self):
        """The price of an ongoing seat is negotiated, not self-served."""
        self.assertEqual(self.create(by=self.buyer).status_code, 403)

    def test_a_client_with_no_company_is_refused(self):
        loner = User.objects.create_user(
            "loner@nowhere.io", "x", role=User.Role.CLIENT)
        response = self.create(client=loner.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("company", str(response.data))

    def test_an_end_before_the_start_is_refused(self):
        self.assertEqual(
            self.create(ends_on="2026-02-01").status_code, 400)

    def test_the_client_sees_it(self):
        self.create()
        rows = as_user(self.buyer).get("/api/engagements").data
        self.assertEqual(len(rows), 1)

    def test_another_companys_client_does_not(self):
        self.create()
        rival_org = Organisation.objects.create(name="Rival", slug="rival-en")
        rival = User.objects.create_user(
            "rival@rival.io", "x", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=rival_org, user=rival, role="owner")
        self.assertEqual(as_user(rival).get("/api/engagements").data, [])

    def test_another_lead_does_not(self):
        self.create()
        self.assertEqual(as_user(self.other_lead).get("/api/engagements").data, [])

    def test_an_expert_sees_the_ones_they_deliver(self):
        e = self.engagement()
        cycle = service.generate_cycle(e, date(2026, 3, 1))
        cycle.experts.add(self.va)
        rows = as_user(self.va).get("/api/engagements").data
        self.assertEqual(len(rows), 1)

    def test_pausing_and_resuming(self):
        e = self.engagement()
        url = f"/api/engagements/{e.id}"
        self.assertEqual(
            as_user(self.lead).post(url, {"action": "pause"}, format="json")
            .data["status"], "paused")
        self.assertEqual(
            as_user(self.lead).post(url, {"action": "resume"}, format="json")
            .data["status"], "active")

    def test_ending_needs_a_reason(self):
        e = self.engagement()
        url = f"/api/engagements/{e.id}"
        self.assertEqual(
            as_user(self.lead).post(url, {"action": "end"}, format="json").status_code,
            400)

    def test_ending_stops_future_billing(self):
        e = self.engagement()
        as_user(self.lead).post(
            f"/api/engagements/{e.id}",
            {"action": "end", "reason": "Client brought it in-house."},
            format="json")
        e.refresh_from_db()
        self.assertEqual(e.status, Engagement.Status.ENDED)
        self.assertFalse(service.due_for_generation(e, date(2026, 3, 1))[0])

    def test_ending_does_not_touch_the_month_already_paid_for(self):
        e = self.engagement()
        cycle = service.generate_cycle(e, date(2026, 3, 1))
        cycle.stage = Project.Stage.IN_PROGRESS
        cycle.save(update_fields=["stage"])
        as_user(self.lead).post(
            f"/api/engagements/{e.id}",
            {"action": "end", "reason": "Done."}, format="json")
        cycle.refresh_from_db()
        self.assertEqual(cycle.stage, Project.Stage.IN_PROGRESS)

    def test_an_ended_retainer_cannot_be_resumed(self):
        e = self.engagement()
        url = f"/api/engagements/{e.id}"
        as_user(self.lead).post(
            url, {"action": "end", "reason": "Done."}, format="json")
        self.assertEqual(
            as_user(self.lead).post(url, {"action": "resume"}, format="json")
            .status_code, 400)

    def test_only_the_running_lead_can_change_it(self):
        e = self.engagement()
        for who in (self.buyer, self.va):
            with self.subTest(who=who.email):
                # 404 for anyone who can't see it at all, 403 for anyone who
                # can but doesn't run it. Both are refusals.
                self.assertIn(
                    as_user(who).post(f"/api/engagements/{e.id}",
                                      {"action": "pause"}, format="json").status_code,
                    (403, 404))

    def test_a_lead_can_raise_a_cycle_by_hand(self):
        """For a retainer set up mid-month that shouldn't wait for the cron."""
        e = self.engagement()
        response = as_user(self.lead).post(
            f"/api/engagements/{e.id}", {"action": "raise-cycle"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(e.cycles.count(), 1)

    def test_raising_by_hand_twice_is_refused(self):
        """Otherwise a lead clicking twice bills a client months ahead."""
        e = self.engagement()
        url = f"/api/engagements/{e.id}"
        as_user(self.lead).post(url, {"action": "raise-cycle"}, format="json")
        response = as_user(self.lead).post(
            url, {"action": "raise-cycle"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("hasn't been paid", str(response.data))
        self.assertEqual(e.cycles.count(), 1)

    def test_a_paused_retainer_cannot_be_raised_by_hand(self):
        e = self.engagement(status=Engagement.Status.PAUSED)
        self.assertEqual(
            as_user(self.lead).post(f"/api/engagements/{e.id}",
                                    {"action": "raise-cycle"},
                                    format="json").status_code, 400)

    def test_everyone_is_told_when_it_ends(self):
        e = self.engagement()
        mail.outbox = []
        as_user(self.lead).post(
            f"/api/engagements/{e.id}",
            {"action": "end", "reason": "Brought in-house."}, format="json")
        to = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.buyer.email, to)
        self.assertIn(self.lead.email, to)


class StandaloneProjectsUnaffectedTests(EngagementTestBase):
    """The regression that would matter most: every project written before."""

    def test_an_ordinary_brief_has_no_engagement(self):
        project = Project.objects.create(
            title="A one-off", client=self.buyer, category="Virtual assistance",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.SUBMITTED, quote_usd=500)
        self.assertIsNone(project.engagement_id)
        self.assertIsNone(project.period_start)

    def test_the_board_shows_cycles_and_briefs_together(self):
        e = self.engagement()
        service.generate_cycle(e, date(2026, 3, 1))
        Project.objects.create(
            title="A one-off", client=self.buyer, category="Virtual assistance",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.SUBMITTED, quote_usd=500)
        rows = as_user(self.lead).get("/api/projects").data
        self.assertEqual(len(rows), 2)


class SimulatedClockTests(EngagementTestBase):
    """The guards have to answer against the date being asked about.

    Found by smoke-testing on real data, not by the suite: the unpaid-cycle
    check originally read the wall clock, so `--on` compared next month's
    cycles against today and found nothing overdue. Every date here is derived
    from `timezone.localdate()` so the tests can't pass by accident the way the
    original hardcoded ones did.
    """

    def test_the_unpaid_guard_uses_the_date_being_asked_about(self):
        start = timezone.localdate() + timedelta(days=40)
        e = self.engagement(started=start, billing_day=1)
        first = service.generate_cycle(e)
        self.assertIsNotNone(first)

        # A month after that period opened, it's still Quoted — and overdue.
        later = first.period_start + timedelta(days=30)
        self.assertIsNotNone(service.blocking_cycle(e, on=later))
        due, why = service.due_for_generation(e, later)
        self.assertFalse(due, "raised the next month over an unpaid one")
        self.assertIn("unpaid", why)

    def test_a_future_cycle_is_not_overdue_from_today(self):
        start = timezone.localdate() + timedelta(days=40)
        e = self.engagement(started=start, billing_day=1)
        service.generate_cycle(e)
        self.assertIsNone(service.blocking_cycle(e))

    def test_a_dry_run_ahead_does_not_promise_to_double_bill(self):
        """The exact shape of the bug: `--on` a month ahead reported raising
        the next cycle while the current one was unpaid."""
        start = timezone.localdate() + timedelta(days=40)
        e = self.engagement(started=start, billing_day=1)
        first = service.generate_cycle(e)

        _, created = service.run(
            dry_run=True, on=first.period_start + timedelta(days=30))
        self.assertEqual(created, [])


class ClientPickerTests(EngagementTestBase):
    """What the retainer form's client field is allowed to offer.

    The picker used to list every client on the platform from `company` — free
    text typed at signup that nothing verifies. Creating the retainer then
    failed on `organisation_memberships`, which is a different question
    entirely, and the rejection was raised inside a modal that painted over the
    toast carrying it. The button looked broken; it was the form being refused
    after the fact.
    """

    def setUp(self):
        super().setUp()
        # Types a company name, holds no seat at one. The old picker showed
        # this person as though they were ready to buy a retainer.
        self.stray = User.objects.create_user(
            "stray@nowhere.io", "x", full_name="Stray Buyer",
            role=User.Role.CLIENT, company="Looks Like A Company Ltd")

    def directory(self, **params):
        return as_user(self.lead).get("/api/users", params).data

    def row(self, rows, user):
        return next(r for r in rows if r["id"] == user.id)

    def test_the_seat_is_reported_not_the_typed_company(self):
        rows = self.directory(role="client")
        self.assertEqual(self.row(rows, self.buyer)["organisation_name"], "Acme Ltd")
        # Free text says otherwise; the field the server checks does not.
        self.assertEqual(self.row(rows, self.stray)["company"],
                         "Looks Like A Company Ltd")
        self.assertEqual(self.row(rows, self.stray)["organisation_name"], "")

    def test_typing_a_name_narrows_the_list(self):
        rows = self.directory(role="client", q="stray")
        self.assertEqual([r["id"] for r in rows], [self.stray.id])

    def test_searching_matches_an_email_fragment_too(self):
        rows = self.directory(role="client", q="enbuyer@")
        self.assertEqual([r["id"] for r in rows], [self.buyer.id])

    def test_a_lead_searching_never_sees_past_clients(self):
        """The search must not become a way to enumerate other leads' experts."""
        rows = self.directory(q="")
        self.assertEqual({r["role"] for r in rows}, {User.Role.CLIENT.value})

    def test_the_client_the_picker_allows_can_actually_be_set_up(self):
        response = as_user(self.lead).post("/api/engagements", {
            "title": "Virtual assistant · 20 hrs",
            "description": "Inbox, calendar and travel.",
            "monthly_amount_usd": "1200", "billing_day": 1,
            "started_on": "2026-09-01", "product_line": self.line.slug,
            "client": self.buyer.id,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_the_one_it_greys_out_is_the_one_the_server_refuses(self):
        """The picker's rule and the server's rule are the same rule."""
        response = as_user(self.lead).post("/api/engagements", {
            "title": "Virtual assistant · 20 hrs",
            "description": "Inbox, calendar and travel.",
            "monthly_amount_usd": "1200", "billing_day": 1,
            "started_on": "2026-09-01", "product_line": self.line.slug,
            "client": self.stray.id,
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("company", str(response.data).lower())


class CycleWorkflowTests(EngagementTestBase):
    """The month-to-month workflow: what carries forward, and what moves on."""

    def _staffed_month(self, period=date(2026, 3, 1)):
        e = self.engagement(amount="1000")
        first = service.generate_cycle(e, period)
        first.experts.add(self.va)
        first.expert = self.va
        first.stage = Project.Stage.COMPLETED
        first.save(update_fields=["expert", "stage"])
        return e, first

    def _pay(self, project):
        from payments import paystack
        from payments.models import Payment

        payment = Payment.objects.create(
            project=project, gateway="paystack", reference=f"RIL-{project.id}",
            access_code="x", amount_subunit=100000, currency="USD",
            usd_total=Decimal(project.quote_usd), status=Payment.Status.PENDING)
        paystack._mark_paid(payment, {})
        project.refresh_from_db()
        return project

    # -- carry forward -------------------------------------------------------

    def test_tasks_carry_forward_as_fresh_to_dos(self):
        e, first = self._staffed_month()
        Task.objects.create(project=first, title="Inbox zero", assignee=self.va,
                            amount_usd=Decimal("300"), status=Task.Status.APPROVED,
                            order=0)
        Task.objects.create(project=first, title="Weekly report", assignee=self.va,
                            amount_usd=Decimal("200"), status=Task.Status.SUBMITTED,
                            order=1)
        nxt = service.generate_cycle(e, date(2026, 4, 1))
        tasks = list(nxt.tasks.order_by("order"))
        self.assertEqual([t.title for t in tasks], ["Inbox zero", "Weekly report"])
        self.assertTrue(all(t.status == Task.Status.TODO for t in tasks))
        self.assertTrue(all(t.assignee_id == self.va.id for t in tasks))
        self.assertEqual([t.amount_usd for t in tasks],
                         [Decimal("300.00"), Decimal("200.00")])

    def test_prices_that_no_longer_fit_carry_unpriced(self):
        e, first = self._staffed_month()
        # Last month had extra scope; this month's pool is only $700.
        Task.objects.create(project=first, title="Big", assignee=self.va,
                            amount_usd=Decimal("900"))
        nxt = service.generate_cycle(e, date(2026, 4, 1))
        task = nxt.tasks.get()
        self.assertEqual(task.amount_usd, Decimal("0"))
        self.assertLessEqual(nxt.allocated_usd, nxt.expert_pool_usd)

    def test_the_referrer_and_share_terms_carry_forward(self):
        bd = User.objects.create_user("enbd@ril.team", "x",
                                      role=User.Role.BUSINESS_DEV)
        self.buyer.referred_by = bd
        self.buyer.save(update_fields=["referred_by"])
        e = self.engagement()
        first = service.generate_cycle(e, date(2026, 3, 1))
        self.assertEqual(first.business_developer, bd)
        first.expert_share_percent = Decimal("65")
        first.save(update_fields=["expert_share_percent"])
        nxt = service.generate_cycle(e, date(2026, 4, 1))
        self.assertEqual(nxt.business_developer, bd)
        self.assertEqual(nxt.expert_share_percent, Decimal("65.00"))

    # -- paying starts the month ---------------------------------------------

    def test_paying_a_staffed_month_starts_delivery(self):
        e, _ = self._staffed_month()
        nxt = self._pay(service.generate_cycle(e, date(2026, 4, 1)))
        self.assertEqual(nxt.stage, Project.Stage.IN_PROGRESS)
        self.assertTrue(any("underway" in m.subject for m in mail.outbox))

    def test_paying_an_unstaffed_month_waits_for_assignment(self):
        e = self.engagement()
        first = self._pay(service.generate_cycle(e, date(2026, 3, 1)))
        self.assertEqual(first.stage, Project.Stage.PAID)

    def test_a_one_off_brief_still_waits_at_paid(self):
        brief = Project.objects.create(
            title="One-off", client=self.buyer, organisation=self.org,
            category="VA", description="…", product_line=self.line,
            lead=self.lead, expert=self.va, stage=Project.Stage.QUOTED,
            quote_usd=500)
        brief.experts.add(self.va)
        self.assertEqual(self._pay(brief).stage, Project.Stage.PAID)

    def test_a_lead_can_start_a_month_stuck_at_paid(self):
        e, _ = self._staffed_month()
        nxt = service.generate_cycle(e, date(2026, 4, 1))
        nxt.stage = Project.Stage.PAID
        nxt.save(update_fields=["stage"])
        resp = as_user(self.lead).post(f"/api/projects/{nxt.id}/start")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["stage"], "In Progress")

    def test_start_refuses_without_a_team_or_payment(self):
        e = self.engagement()
        cycle = service.generate_cycle(e, date(2026, 3, 1))
        url = f"/api/projects/{cycle.id}/start"
        self.assertEqual(as_user(self.lead).post(url).status_code, 400)  # unpaid
        cycle.stage = Project.Stage.PAID
        cycle.save(update_fields=["stage"])
        self.assertEqual(as_user(self.lead).post(url).status_code, 400)  # no team

    # -- the lead can see the work -------------------------------------------

    def test_the_lead_sees_where_each_months_work_stands(self):
        e, first = self._staffed_month()
        for status in (Task.Status.TODO, Task.Status.SUBMITTED,
                       Task.Status.CHANGES, Task.Status.APPROVED):
            Task.objects.create(project=first, title=status, assignee=self.va,
                                status=status)
        Task.objects.create(project=first, title="Nobody's", status=Task.Status.TODO)
        data = as_user(self.lead).get(f"/api/engagements/{e.id}").data
        month = data["cycles"][0]
        self.assertEqual(month["task_summary"], {
            "total": 5, "todo": 2, "submitted": 1, "changes": 1,
            "approved": 1, "unassigned": 1})
        self.assertEqual(month["team"], ["A VA"])

    def test_the_board_carries_the_task_breakdown(self):
        e, first = self._staffed_month()
        Task.objects.create(project=first, title="t", assignee=self.va,
                            status=Task.Status.SUBMITTED)
        row = next(r for r in as_user(self.lead).get("/api/projects").data
                   if r["id"] == first.id)
        self.assertEqual(row["task_summary"]["submitted"], 1)
        self.assertEqual(row["engagement"], e.id)

    # -- ending --------------------------------------------------------------

    def test_ending_withdraws_the_unpaid_month(self):
        e, first = self._staffed_month()
        nxt = service.generate_cycle(e, date(2026, 4, 1))
        mail.outbox.clear()
        as_user(self.lead).post(f"/api/engagements/{e.id}",
                                {"action": "end", "reason": "In-house now."},
                                format="json")
        nxt.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(nxt.stage, Project.Stage.CANCELLED)
        self.assertEqual(first.stage, Project.Stage.COMPLETED)
        ended = next(m for m in mail.outbox if "Retainer ended" in m.subject)
        body = ended.body + "".join(a[0] for a in getattr(ended, "alternatives", []))
        self.assertIn("withdrawn", body)
        self.assertNotIn("it's paid for", body)

    # -- lead changes --------------------------------------------------------

    def test_a_lead_change_on_a_month_moves_the_retainer(self):
        from projects.models import LeadChangeRequest

        e = self.engagement()
        cycle = service.generate_cycle(e, date(2026, 3, 1))
        entry = LeadChangeRequest.objects.create(
            project=cycle, requested_by=self.buyer, reason="Unresponsive.",
            previous_lead=self.lead)
        resp = as_user(self.admin).post(
            f"/api/lead-changes/{entry.id}/resolve",
            {"decision": "reassign", "new_lead": self.other_lead.id},
            format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        e.refresh_from_db()
        self.assertEqual(e.lead, self.other_lead)
        nxt = service.generate_cycle(e, date(2026, 4, 1))
        self.assertEqual(nxt.lead, self.other_lead)
