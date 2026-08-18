"""The client's private verdict (G4).

There was no rating, review or satisfaction capture anywhere. Internalising
quality to the lead's vetting is a coherent choice — public stars would turn
the roster into a leaderboard and hand a departing lead a portable reputation
— but going that far and capturing the client's opinion *nowhere* left no way
to tell a good lead from a lucky one. On-time rate measures delivery; it does
not measure whether anyone was happy.

These tests pin the read boundary, which is the whole design: the lead sees it,
the experts never do.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import ProductLine
from projects.models import Project, ProjectFeedback

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class FeedbackTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "fblead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.other_lead = User.objects.create_user(
            "fbother@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.other_lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "fbexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "fbclient@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)
        self.admin = User.objects.create_superuser("fbboss@ril.team", "x")

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.COMPLETED, quote_usd=4000,
            completed_at=timezone.now())
        self.project.experts.add(self.expert)

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    def leave(self, by=None, **payload):
        body = {"rating": 5, "comment": "Great work.", "would_work_again": True}
        body.update(payload)
        return as_user(by or self.customer).post(
            self.url("/feedback"), body, format="json")

    # --- leaving it ---
    def test_the_client_can_rate_finished_work(self):
        response = self.leave()
        self.assertEqual(response.status_code, 200, response.data)
        entry = ProjectFeedback.objects.get()
        self.assertEqual(entry.rating, 5)
        self.assertEqual(entry.comment, "Great work.")
        self.assertTrue(entry.would_work_again)

    def test_a_comment_is_optional(self):
        self.assertEqual(self.leave(comment="").status_code, 200)

    def test_the_rating_is_bounded(self):
        for rating in (0, 6, -1):
            with self.subTest(rating=rating):
                self.assertEqual(self.leave(rating=rating).status_code, 400)

    def test_only_once(self):
        self.leave()
        self.assertEqual(self.leave().status_code, 400)
        self.assertEqual(ProjectFeedback.objects.count(), 1)

    def test_only_the_client(self):
        for who in (self.lead, self.expert, self.admin):
            with self.subTest(who=who.email):
                self.assertIn(self.leave(by=who).status_code, (403, 404))

    def test_only_on_finished_work(self):
        for stage in (Project.Stage.SUBMITTED, Project.Stage.QUOTED,
                      Project.Stage.PAID, Project.Stage.IN_PROGRESS,
                      Project.Stage.REVIEW):
            with self.subTest(stage=stage):
                self.project.stage = stage
                self.project.save(update_fields=["stage"])
                self.assertEqual(self.leave().status_code, 400)

    def test_a_cancelled_project_can_still_be_rated(self):
        """Where the reason matters most, and nobody would otherwise ask."""
        self.project.stage = Project.Stage.CANCELLED
        self.project.save(update_fields=["stage"])
        self.assertEqual(self.leave(rating=2).status_code, 200)

    # --- who may read it ---
    def test_the_lead_running_it_can_read_it(self):
        self.leave(rating=4, comment="Solid.")
        detail = as_user(self.lead).get(self.url()).data
        self.assertEqual(detail["feedback"]["rating"], 4)
        self.assertEqual(detail["feedback"]["comment"], "Solid.")

    def test_the_expert_who_delivered_it_never_sees_it(self):
        """The point of the whole design."""
        self.leave(rating=2, comment="Disappointing.")
        detail = as_user(self.expert).get(self.url()).data
        self.assertIsNone(detail["feedback"])

    def test_another_lead_never_sees_it(self):
        self.leave()
        response = as_user(self.other_lead).get(self.url())
        if response.status_code == 200:
            self.assertIsNone(response.data["feedback"])

    def test_the_client_can_read_back_what_they_wrote(self):
        self.leave(rating=3)
        detail = as_user(self.customer).get(self.url()).data
        self.assertEqual(detail["feedback"]["rating"], 3)

    def test_an_admin_can_read_it(self):
        self.leave()
        detail = as_user(self.admin).get(self.url()).data
        self.assertEqual(detail["feedback"]["rating"], 5)

    # --- prompting ---
    def test_the_client_is_told_they_still_owe_one(self):
        detail = as_user(self.customer).get(self.url()).data
        self.assertTrue(detail["can_leave_feedback"])
        self.leave()
        detail = as_user(self.customer).get(self.url()).data
        self.assertFalse(detail["can_leave_feedback"])

    def test_nobody_else_is_prompted(self):
        for who in (self.lead, self.expert, self.admin):
            with self.subTest(who=who.email):
                response = as_user(who).get(self.url())
                if response.status_code == 200:
                    self.assertFalse(response.data["can_leave_feedback"])

    def test_a_running_project_prompts_nobody(self):
        self.project.stage = Project.Stage.IN_PROGRESS
        self.project.save(update_fields=["stage"])
        detail = as_user(self.customer).get(self.url()).data
        self.assertFalse(detail["can_leave_feedback"])

    # --- notification ---
    def test_only_the_lead_is_emailed(self):
        mail.outbox = []
        self.leave(rating=2, comment="Not great.")
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertEqual(recipients, {self.lead.email})

    def test_the_feed_never_carries_it(self):
        """The activity feed is read by the experts."""
        before = self.project.activity.count()
        self.leave(rating=1, comment="Bad.")
        self.assertEqual(self.project.activity.count(), before)

    # --- reporting ---
    def test_it_reaches_the_lead_scorecard(self):
        from projects import reports

        self.leave(rating=4)
        row = next(r for r in reports.delivery_leads() if r["id"] == self.lead.id)
        self.assertEqual(row["avg_rating"], 4.0)
        self.assertEqual(row["rating_sample"], 1)
        self.assertEqual(row["would_repeat_percent"], 100.0)

    def test_an_unrated_lead_reads_null_not_zero(self):
        """A lead nobody has rated is not a lead rated badly."""
        from projects import reports

        row = next(r for r in reports.delivery_leads() if r["id"] == self.lead.id)
        self.assertIsNone(row["avg_rating"])
        self.assertEqual(row["rating_sample"], 0)

    def test_the_sample_size_is_always_reported(self):
        """One 5/5 and forty 4.6s must not read as the same thing."""
        from projects import reports

        self.leave(rating=5)
        row = next(r for r in reports.delivery_leads() if r["id"] == self.lead.id)
        self.assertIn("rating_sample", row)
        self.assertEqual(row["rating_sample"], 1)
