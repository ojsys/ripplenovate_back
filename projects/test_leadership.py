"""Changing a delivery lead, and the public leaderboard.

Both of these move the platform's posture. The client never chose their lead —
that's what removes the shortlisting — and these two features hand some of that
choice back, deliberately.

The tests are mostly about the edges that keeps honest: who may ask, who may
grant, what happens to the money, and what a leaderboard is allowed to claim
from a sample of two.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments.models import Earning
from projects import leaderboard as board
from projects.models import LeadChangeRequest, Project, ProjectFeedback

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class LeadChangeTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "lclead@ril.team", "x", full_name="Current Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.successor = User.objects.create_user(
            "lcnext@ril.team", "x", full_name="Better Lead",
            role=User.Role.DELIVERY_LEAD)
        self.successor.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "lcexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.buyer = User.objects.create_user(
            "lcbuyer@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)
        self.admin = User.objects.create_superuser("lcboss@ril.team", "x")

        self.project = Project.objects.create(
            title="A rebrand", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=10000)
        self.project.experts.add(self.expert)

    def ask(self, by=None, reason="No updates for three weeks."):
        return as_user(by or self.buyer).post(
            f"/api/projects/{self.project.id}/lead-change",
            {"reason": reason}, format="json")

    def resolve(self, entry, by=None, **payload):
        return as_user(by or self.admin).post(
            f"/api/lead-changes/{entry.id}/resolve", payload, format="json")

    # --- asking ---
    def test_a_client_can_ask(self):
        response = self.ask()
        self.assertEqual(response.status_code, 201, response.data)
        entry = LeadChangeRequest.objects.get()
        self.assertEqual(entry.previous_lead_id, self.lead.id)
        self.assertTrue(entry.is_open)

    def test_a_reason_is_required(self):
        self.assertEqual(self.ask(reason="   ").status_code, 400)

    def test_only_the_client(self):
        for who in (self.lead, self.expert, self.successor):
            with self.subTest(who=who.email):
                self.assertIn(self.ask(by=who).status_code, (403, 404))

    def test_asking_twice_is_refused(self):
        self.ask()
        self.assertEqual(self.ask().status_code, 400)

    def test_a_finished_project_cannot_be_reassigned(self):
        for stage in (Project.Stage.COMPLETED, Project.Stage.CANCELLED):
            with self.subTest(stage=stage):
                self.project.stage = stage
                self.project.save(update_fields=["stage"])
                self.assertEqual(self.ask().status_code, 400)

    def test_the_lead_being_complained_about_is_not_emailed(self):
        """They hear it from a person once somebody has decided what to do."""
        mail.outbox = []
        self.ask()
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.admin.email, recipients)
        self.assertNotIn(self.lead.email, recipients)

    def test_it_is_not_written_to_the_activity_feed(self):
        """The outgoing lead reads that feed."""
        before = self.project.activity.count()
        self.ask()
        self.assertEqual(self.project.activity.count(), before)

    # --- resolving ---
    def test_an_admin_reassigns(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        response = self.resolve(entry, decision="reassign",
                                new_lead=self.successor.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.lead_id, self.successor.id)
        entry.refresh_from_db()
        self.assertEqual(entry.status, LeadChangeRequest.Status.REASSIGNED)

    def test_declining_needs_a_reason(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        self.assertEqual(self.resolve(entry, decision="decline").status_code, 400)
        response = self.resolve(entry, decision="decline",
                                note="We've spoken to them; give it a week.")
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.lead_id, self.lead.id)

    def test_only_an_admin_resolves(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        for who in (self.lead, self.successor, self.buyer):
            with self.subTest(who=who.email):
                self.assertEqual(
                    self.resolve(entry, by=who, decision="reassign",
                                 new_lead=self.successor.id).status_code, 403)

    def test_the_same_lead_cannot_be_reassigned_to(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        self.assertEqual(
            self.resolve(entry, decision="reassign",
                         new_lead=self.lead.id).status_code, 400)

    def test_resolving_twice_is_refused(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        self.resolve(entry, decision="reassign", new_lead=self.successor.id)
        self.assertEqual(
            self.resolve(entry, decision="decline", note="…").status_code, 400)

    def test_the_queue_is_admin_only(self):
        self.ask()
        self.assertEqual(as_user(self.buyer).get("/api/lead-changes").status_code, 403)
        self.assertEqual(as_user(self.lead).get("/api/lead-changes").status_code, 403)
        self.assertEqual(len(as_user(self.admin).get("/api/lead-changes").data), 1)

    # --- the money ---
    def test_the_incoming_lead_earns_the_share(self):
        """Earnings credit at completion from whoever holds the project then."""
        self.ask()
        entry = LeadChangeRequest.objects.get()
        self.resolve(entry, decision="reassign", new_lead=self.successor.id)

        self.project.refresh_from_db()
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        as_user(self.buyer).post(f"/api/projects/{self.project.id}/approve")

        rows = Earning.objects.filter(kind=Earning.Kind.DELIVERY_LEAD)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().user_id, self.successor.id)

    def test_the_response_says_what_it_did_to_the_money(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        response = self.resolve(entry, decision="reassign",
                                new_lead=self.successor.id)
        self.assertIn("earns the delivery-lead share",
                      response.data["lead_share_note"])

    def test_anything_already_earned_is_untouched(self):
        earned = Earning.objects.create(
            user=self.lead, project=self.project,
            kind=Earning.Kind.DELIVERY_LEAD,
            share_percent=Decimal("15"), amount_usd=Decimal("500"))
        self.ask()
        entry = LeadChangeRequest.objects.get()
        self.resolve(entry, decision="reassign", new_lead=self.successor.id)
        earned.refresh_from_db()
        self.assertEqual(earned.user_id, self.lead.id)

    def test_the_delivery_team_is_not_disturbed(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        self.resolve(entry, decision="reassign", new_lead=self.successor.id)
        self.project.refresh_from_db()
        self.assertEqual([e.id for e in self.project.experts.all()],
                         [self.expert.id])

    # --- who hears ---
    def test_everybody_is_told_when_it_moves(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        mail.outbox = []
        self.resolve(entry, decision="reassign", new_lead=self.successor.id)
        to = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.buyer.email, to)
        self.assertIn(self.successor.email, to)
        self.assertIn(self.lead.email, to, "the outgoing lead should hear it")

    def test_the_outgoing_lead_is_told_their_earnings_are_safe(self):
        self.ask()
        entry = LeadChangeRequest.objects.get()
        mail.outbox = []
        self.resolve(entry, decision="reassign", new_lead=self.successor.id)
        theirs = [m for m in mail.outbox if self.lead.email in m.to]
        self.assertIn("already earned", " ".join(m.body for m in theirs).lower())


class LeaderboardTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.buyer = User.objects.create_user(
            "lbbuyer@acme.io", "x", role=User.Role.CLIENT)

    def lead_with(self, email, *, delivered=0, ratings=(), on_time=True,
                  listed=True):
        person = User.objects.create_user(
            email, "x", full_name=email.split("@")[0].title(),
            role=User.Role.DELIVERY_LEAD, show_in_leaderboard=listed)
        person.product_lines.add(self.line)
        target = timezone.localdate() - timezone.timedelta(days=1) \
            if hasattr(timezone, "timedelta") else None
        from datetime import timedelta as td
        target = timezone.localdate() + (td(days=1) if on_time else td(days=-30))
        for i in range(delivered):
            project = Project.objects.create(
                title=f"P{i}", client=self.buyer, category="Brand identity",
                description="…", product_line=self.line, lead=person,
                stage=Project.Stage.COMPLETED, quote_usd=1000,
                target_date=target, completed_at=timezone.now())
            if i < len(ratings):
                ProjectFeedback.objects.create(
                    project=project, rating=ratings[i], comment="…")
        return person

    def public(self, **params):
        return APIClient().get("/api/leaderboard", params).data

    # --- the floor ---
    def test_too_little_delivered_work_means_no_ranking(self):
        """One project and one five-star review is luck, not a record."""
        self.lead_with("thin@ril.team", delivered=board.MIN_DELIVERED - 1,
                       ratings=(5,) * 5)
        self.assertEqual(self.public()["leads"], [])

    def test_at_the_floor_they_appear(self):
        self.lead_with("solid@ril.team", delivered=board.MIN_DELIVERED)
        self.assertEqual(len(self.public()["leads"]), 1)

    def test_a_rating_needs_its_own_sample(self):
        """Delivered enough to be ranked, not rated enough to show a number."""
        self.lead_with("unrated@ril.team", delivered=5, ratings=(5,))
        row = self.public()["leads"][0]
        self.assertIsNone(row["rating"])
        self.assertEqual(row["rating_sample"], 1)

    def test_a_rating_shows_once_there_are_enough(self):
        self.lead_with("rated@ril.team", delivered=5, ratings=(5, 4, 5))
        row = self.public()["leads"][0]
        self.assertEqual(row["rating"], 4.7)
        self.assertEqual(row["rating_sample"], 3)

    # --- ordering ---
    def test_better_rated_leads_rank_higher(self):
        self.lead_with("good@ril.team", delivered=5, ratings=(5, 5, 5))
        self.lead_with("poor@ril.team", delivered=5, ratings=(2, 2, 2))
        names = [r["name"] for r in self.public()["leads"]]
        self.assertEqual(names[0], "Good")

    def test_lateness_costs_you(self):
        self.lead_with("prompt@ril.team", delivered=5, ratings=(4, 4, 4),
                       on_time=True)
        self.lead_with("late@ril.team", delivered=5, ratings=(4, 4, 4),
                       on_time=False)
        self.assertEqual(self.public()["leads"][0]["name"], "Prompt")

    def test_volume_alone_does_not_win(self):
        """Otherwise the leaderboard just ranks whoever has been here longest."""
        self.lead_with("busy@ril.team", delivered=20, ratings=(2, 2, 2))
        self.lead_with("loved@ril.team", delivered=4, ratings=(5, 5, 5))
        self.assertEqual(self.public()["leads"][0]["name"], "Loved")

    # --- the opt-out ---
    def test_somebody_who_opted_out_is_left_off(self):
        self.lead_with("shy@ril.team", delivered=5, ratings=(5, 5, 5),
                       listed=False)
        self.assertEqual(self.public()["leads"], [])

    def test_an_admin_view_still_shows_them(self):
        self.lead_with("shy@ril.team", delivered=5, ratings=(5, 5, 5),
                       listed=False)
        self.assertEqual(len(board.leads(public=False)), 1)

    # --- what it exposes ---
    def test_it_is_readable_without_signing_in(self):
        """The people this is for haven't got an account yet."""
        self.lead_with("solid@ril.team", delivered=5)
        response = APIClient().get("/api/leaderboard")
        self.assertEqual(response.status_code, 200)

    def test_every_number_travels_with_the_row(self):
        """A ranking whose inputs are hidden gets gamed, not trusted."""
        self.lead_with("solid@ril.team", delivered=5, ratings=(5, 4, 4))
        row = self.public()["leads"][0]
        for key in ("delivered", "rating", "rating_sample", "on_time_percent",
                    "score"):
            self.assertIn(key, row)

    def test_no_email_address_is_published(self):
        self.lead_with("solid@ril.team", delivered=5)
        self.assertNotIn("@", str(self.public()["leads"]))

    def test_it_can_be_narrowed_to_a_discipline(self):
        self.lead_with("solid@ril.team", delivered=5)
        self.assertEqual(
            len(self.public(product_line="design-creative")["leads"]), 1)
        self.assertEqual(
            len(self.public(product_line="software-web")["leads"]), 0)


class RequestALeadTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "rllead@ril.team", "x", full_name="Wanted Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.buyer = User.objects.create_user(
            "rlbuyer@acme.io", "x", role=User.Role.CLIENT)
        self.other = User.objects.create_user(
            "rlother@acme.io", "x", role=User.Role.CLIENT)
        self.brief = Project.objects.create(
            title="A new brief", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line,
            stage=Project.Stage.SUBMITTED, quote_usd=0)

    def ask(self, by=None, project=None, lead=None):
        return as_user(by or self.buyer).post(
            "/api/request-lead",
            {"project": (project or self.brief).id, "lead": (lead or self.lead).id},
            format="json")

    def test_a_client_can_ask_for_somebody_on_an_unclaimed_brief(self):
        response = self.ask()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.brief.activity.filter(
            text__contains="Wanted Lead").exists())

    def test_the_lead_is_told(self):
        mail.outbox = []
        self.ask()
        self.assertIn(self.lead.email,
                      {addr for m in mail.outbox for addr in m.to})

    def test_it_is_a_request_not_an_assignment(self):
        """The intake queue still works the way it always did."""
        self.ask()
        self.brief.refresh_from_db()
        self.assertIsNone(self.brief.lead_id)

    def test_a_brief_that_already_has_a_lead_is_refused(self):
        self.brief.lead = self.lead
        self.brief.save(update_fields=["lead"])
        response = self.ask()
        self.assertEqual(response.status_code, 400)
        self.assertIn("already has a delivery lead", str(response.data))

    def test_you_cannot_ask_on_somebody_elses_brief(self):
        self.assertEqual(self.ask(by=self.other).status_code, 404)
