"""Terms, and letting a delivery lead go (G10).

The platform's structural defence against work going off-platform is genuinely
strong — a client never chooses their expert and often doesn't know who did the
work. But the risk that remains sits with the **lead**, not the expert: they own
the client relationship and the roster, and until now one leaving left orphans
behind them. Experts with a dangling lead, live projects owned by somebody who
no longer works here, and retainers billing into nobody's inbox.

The rule these tests exist to protect: **moving a book never restates who was
paid.** Completed work keeps the lead who delivered it, and no earning moves.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import (
    Organisation,
    OrganisationMember,
    TermsAcceptance,
)
from catalog.models import ProductLine
from payments.models import Earning
from projects.models import Engagement, Project

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class TermsTests(TestCase):
    def setUp(self):
        self.lead = User.objects.create_user(
            "tlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.buyer = User.objects.create_user(
            "tbuyer@acme.io", "x", role=User.Role.CLIENT)

    @override_settings(TERMS_VERSION="2026-08")
    def test_a_partner_starts_unaccepted(self):
        data = as_user(self.lead).get("/api/terms").data
        self.assertEqual(data["version"], "2026-08")
        self.assertFalse(data["accepted"])
        self.assertTrue(data["required"])

    @override_settings(TERMS_VERSION="2026-08")
    def test_accepting_is_recorded_with_the_version(self):
        response = as_user(self.lead).post("/api/terms")
        self.assertTrue(response.data["accepted"])
        row = TermsAcceptance.objects.get()
        self.assertEqual(row.user_id, self.lead.id)
        self.assertEqual(row.version, "2026-08")

    @override_settings(TERMS_VERSION="2026-08")
    def test_accepting_twice_writes_one_row(self):
        as_user(self.lead).post("/api/terms")
        as_user(self.lead).post("/api/terms")
        self.assertEqual(TermsAcceptance.objects.count(), 1)

    def test_a_new_version_has_to_be_accepted_again(self):
        """"They agreed" is worthless without "to what"."""
        with override_settings(TERMS_VERSION="2026-08"):
            as_user(self.lead).post("/api/terms")
        with override_settings(TERMS_VERSION="2027-01"):
            data = as_user(self.lead).get("/api/terms").data
            self.assertFalse(data["accepted"])
            as_user(self.lead).post("/api/terms")
        self.assertEqual(TermsAcceptance.objects.count(), 2)

    def test_a_client_is_not_gated(self):
        """A client's agreement is a signup checkbox. Non-circumvention terms
        are the delivery side's."""
        self.assertFalse(as_user(self.buyer).get("/api/terms").data["required"])

    def test_an_old_acceptance_is_kept_not_overwritten(self):
        with override_settings(TERMS_VERSION="2026-08"):
            as_user(self.lead).post("/api/terms")
        with override_settings(TERMS_VERSION="2027-01"):
            as_user(self.lead).post("/api/terms")
        self.assertTrue(TermsAcceptance.objects.filter(version="2026-08").exists())


class OffboardingTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.other_line = ProductLine.objects.get(slug="software-web")
        self.admin = User.objects.create_superuser("offboss@ril.team", "x")

        self.leaving = User.objects.create_user(
            "offleaving@ril.team", "x", full_name="Departing Lead",
            role=User.Role.DELIVERY_LEAD)
        self.leaving.product_lines.add(self.line)
        self.successor = User.objects.create_user(
            "offnext@ril.team", "x", full_name="New Lead",
            role=User.Role.DELIVERY_LEAD)
        self.successor.product_lines.add(self.line, self.other_line)

        self.expert = User.objects.create_user(
            "offexpert@ril.dev", "x", full_name="An Expert",
            role=User.Role.EXPERT, lead=self.leaving)
        self.expert.product_lines.add(self.line)

        self.org = Organisation.objects.create(name="Acme", slug="acme-off")
        self.buyer = User.objects.create_user(
            "offbuyer@acme.io", "x", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=self.org, user=self.buyer, role="owner")

    def project(self, stage, quote=1000):
        return Project.objects.create(
            title=f"Work {stage}", client=self.buyer, organisation=self.org,
            category="Brand identity", description="…", product_line=self.line,
            lead=self.leaving, expert=self.expert, stage=stage, quote_usd=quote,
            completed_at=timezone.now() if stage == Project.Stage.COMPLETED else None)

    def offboard(self, by=None, successor=None):
        return as_user(by or self.admin).post(
            f"/api/users/{self.leaving.id}/offboard",
            {"successor": (successor or self.successor).id}, format="json")

    # --- the move ---
    def test_the_roster_moves(self):
        response = self.offboard()
        self.assertEqual(response.status_code, 200, response.data)
        self.expert.refresh_from_db()
        self.assertEqual(self.expert.lead_id, self.successor.id)
        self.assertEqual(response.data["experts_moved"], 1)

    def test_the_roster_is_widened_so_nobody_becomes_unassignable(self):
        self.offboard()
        slugs = set(self.expert.product_lines.values_list("slug", flat=True))
        self.assertIn(self.other_line.slug, slugs)
        self.assertIn(self.line.slug, slugs, "lost what they already covered")

    def test_live_projects_move(self):
        live = [self.project(s) for s in (Project.Stage.PAID,
                                          Project.Stage.IN_PROGRESS,
                                          Project.Stage.REVIEW)]
        response = self.offboard()
        self.assertEqual(response.data["projects_moved"], 3)
        for project in live:
            project.refresh_from_db()
            self.assertEqual(project.lead_id, self.successor.id)

    def test_completed_work_keeps_the_lead_who_delivered_it(self):
        """Rewriting it would misattribute history and disagree with the ledger."""
        done = self.project(Project.Stage.COMPLETED)
        self.offboard()
        done.refresh_from_db()
        self.assertEqual(done.lead_id, self.leaving.id)

    def test_cancelled_work_is_left_alone_too(self):
        killed = self.project(Project.Stage.CANCELLED)
        self.offboard()
        killed.refresh_from_db()
        self.assertEqual(killed.lead_id, self.leaving.id)

    def test_live_retainers_move(self):
        engagement = Engagement.objects.create(
            organisation=self.org, client=self.buyer, lead=self.leaving,
            product_line=self.line, title="A retainer", description="…",
            monthly_amount_usd=Decimal("500"), started_on=date.today())
        response = self.offboard()
        self.assertEqual(response.data["retainers_moved"], 1)
        engagement.refresh_from_db()
        self.assertEqual(engagement.lead_id, self.successor.id)

    def test_an_ended_retainer_is_left_alone(self):
        engagement = Engagement.objects.create(
            organisation=self.org, client=self.buyer, lead=self.leaving,
            product_line=self.line, title="Old retainer", description="…",
            monthly_amount_usd=Decimal("500"), started_on=date.today(),
            status=Engagement.Status.ENDED)
        self.offboard()
        engagement.refresh_from_db()
        self.assertEqual(engagement.lead_id, self.leaving.id)

    # --- the money ---
    def test_no_earning_moves(self):
        """The rule the whole thing rests on: money already credited belongs to
        whoever earned it."""
        done = self.project(Project.Stage.COMPLETED)
        earning = Earning.objects.create(
            user=self.leaving, project=done,
            kind=Earning.Kind.DELIVERY_LEAD,
            share_percent=Decimal("15"), amount_usd=Decimal("150"))
        self.offboard()
        earning.refresh_from_db()
        self.assertEqual(earning.user_id, self.leaving.id)
        self.assertEqual(earning.amount_usd, Decimal("150.00"))

    def test_an_outstanding_balance_is_reported_not_settled(self):
        """Paying somebody out has a two-person rule on it and doesn't belong
        inside a bulk move."""
        done = self.project(Project.Stage.COMPLETED)
        Earning.objects.create(
            user=self.leaving, project=done,
            kind=Earning.Kind.DELIVERY_LEAD,
            share_percent=Decimal("15"), amount_usd=Decimal("150"))
        response = self.offboard()
        self.assertEqual(
            Decimal(response.data["outstanding_balance_usd"]), Decimal("150.00"))
        self.assertEqual(
            Earning.objects.filter(user=self.leaving).count(), 1,
            "the balance was altered rather than reported")

    # --- who may do it ---
    def test_only_an_admin(self):
        for who in (self.successor, self.expert, self.buyer):
            with self.subTest(who=who.email):
                self.assertEqual(self.offboard(by=who).status_code, 403)

    def test_a_successor_is_required(self):
        response = as_user(self.admin).post(
            f"/api/users/{self.leaving.id}/offboard", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("another delivery lead", str(response.data))

    def test_the_successor_has_to_be_a_lead(self):
        response = as_user(self.admin).post(
            f"/api/users/{self.leaving.id}/offboard",
            {"successor": self.expert.id}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_they_cannot_be_handed_to_themselves(self):
        self.assertEqual(self.offboard(successor=self.leaving).status_code, 400)

    def test_an_unapproved_successor_is_refused(self):
        pending = User.objects.create_user(
            "offpending@ril.team", "x", role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING)
        response = self.offboard(successor=pending)
        self.assertEqual(response.status_code, 400)
        self.assertIn("being reviewed", str(response.data))

    def test_offboarding_somebody_who_is_not_a_lead_is_a_404(self):
        response = as_user(self.admin).post(
            f"/api/users/{self.expert.id}/offboard",
            {"successor": self.successor.id}, format="json")
        self.assertEqual(response.status_code, 404)

    # --- who hears ---
    def test_the_successor_and_the_experts_are_told(self):
        mail.outbox = []
        self.offboard()
        to = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.successor.email, to)
        self.assertIn(self.expert.email, to,
                      "an expert should not learn this by noticing")

    def test_the_experts_are_told_their_earnings_are_safe(self):
        mail.outbox = []
        self.offboard()
        theirs = [m for m in mail.outbox if self.expert.email in m.to]
        self.assertTrue(theirs)
        self.assertIn("earned", " ".join(m.body for m in theirs).lower())


class AdminGuideTests(TestCase):
    """The admin help content, served rather than bundled.

    It lives on the server because everything in the frontend's `guide.js`
    ships in the JavaScript bundle and is readable by anyone who opens
    devtools — gating it in the UI alone would have been a gesture. This is the
    check that actually holds.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser("agboss@ril.team", "x")
        self.lead = User.objects.create_user(
            "aglead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "agexpert@ril.dev", "x", role=User.Role.EXPERT)
        self.buyer = User.objects.create_user(
            "agbuyer@acme.io", "x", role=User.Role.CLIENT)

    def test_an_admin_gets_the_content(self):
        response = as_user(self.admin).get("/api/guide/admin")
        self.assertEqual(response.status_code, 200)
        entries = response.data["entries"]
        ids = {e["id"] for e in entries}
        self.assertIn("admin-impersonate", ids)
        self.assertIn("admin-offboard", ids)
        self.assertIn("admin-settings", ids)

    def test_nobody_else_does(self):
        for who in (self.lead, self.expert, self.buyer):
            with self.subTest(who=who.email):
                self.assertEqual(
                    as_user(who).get("/api/guide/admin").status_code, 403)

    def test_a_signed_out_visitor_does_not(self):
        self.assertIn(APIClient().get("/api/guide/admin").status_code, (401, 403))

    def test_staff_alone_is_not_enough(self):
        """Same rule as everywhere else — staff see the books, not the levers."""
        staffer = User.objects.create_user(
            "agstaff@ril.team", "x", role=User.Role.DELIVERY_LEAD, is_staff=True)
        self.assertEqual(
            as_user(staffer).get("/api/guide/admin").status_code, 403)

    def test_every_entry_has_the_shape_the_page_renders(self):
        """The page renders server entries and bundled ones identically, so a
        malformed one here is a blank card rather than an error."""
        entries = as_user(self.admin).get("/api/guide/admin").data["entries"]
        for entry in entries:
            with self.subTest(entry=entry.get("id")):
                self.assertTrue(entry.get("id"))
                self.assertTrue(entry.get("section"))
                self.assertTrue(entry.get("q"))
                self.assertIsInstance(entry.get("a", []), list)
                self.assertIsInstance(entry.get("list", []), list)
