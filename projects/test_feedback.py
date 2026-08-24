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
from projects import reviews as reviews_service
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


class PublicReviewTests(TestCase):
    """Which reviews may be shown in public, and which never may.

    Every review is written under a promise printed on the form: it goes to the
    delivery lead and nobody else. Publishing on the strength of that would
    break the promise the words were written under, so consent is a separate,
    explicit yes — and the default is no.
    """

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "prlead@ril.team", "x", full_name="Ngozi Adeyemi",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "prexpert@ril.dev", "x", full_name="Zainab Bello",
            role=User.Role.EXPERT, lead=self.lead)
        self.buyer = User.objects.create_user(
            "prbuyer@acme.io", "x", full_name="Amara Okafor",
            role=User.Role.CLIENT)

    def rated(self, *, rating=5, comment="Excellent work.", publish=False,
              company="HopeBridge Foundation"):
        project = Project.objects.create(
            title="A rebrand", client=self.buyer, category="Brand identity",
            company=company, description="…", product_line=self.line,
            lead=self.lead, expert=self.expert,
            stage=Project.Stage.COMPLETED, quote_usd=1000,
            completed_at=timezone.now())
        return ProjectFeedback.objects.create(
            project=project, rating=rating, comment=comment,
            would_work_again=True, may_publish=publish)

    def public(self, **params):
        return APIClient().get("/api/reviews", params).data

    # --- consent ---
    def test_consent_defaults_to_no(self):
        self.assertFalse(self.rated().may_publish)

    def test_an_unconsented_review_is_never_published(self):
        self.rated(comment="Please don't quote me.", publish=False)
        self.assertEqual(self.public()["reviews"], [])

    def test_a_consented_one_is(self):
        self.rated(comment="They were excellent.", publish=True)
        reviews = self.public()["reviews"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["comment"], "They were excellent.")

    def test_a_client_can_give_consent_through_the_form(self):
        project = Project.objects.create(
            title="Another", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.COMPLETED, quote_usd=500,
            completed_at=timezone.now())
        as_user(self.buyer).post(
            f"/api/projects/{project.id}/feedback",
            {"rating": 5, "comment": "Great.", "may_publish": True},
            format="json")
        self.assertTrue(ProjectFeedback.objects.get(project=project).may_publish)

    def test_leaving_it_out_means_no(self):
        """A missing field must never read as consent."""
        project = Project.objects.create(
            title="Another", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.COMPLETED, quote_usd=500,
            completed_at=timezone.now())
        as_user(self.buyer).post(
            f"/api/projects/{project.id}/feedback",
            {"rating": 5, "comment": "Great."}, format="json")
        self.assertFalse(ProjectFeedback.objects.get(project=project).may_publish)

    def test_a_bare_rating_is_not_a_testimonial(self):
        self.rated(comment="", publish=True)
        self.assertEqual(self.public()["reviews"], [])

    # --- what a public review may contain ---
    def test_it_names_the_company_not_the_person(self):
        self.rated(publish=True)
        review = self.public()["reviews"][0]
        self.assertEqual(review["company"], "HopeBridge Foundation")
        self.assertNotIn("Amara", str(review))

    def test_it_never_names_the_delivery_team(self):
        """The restraint that keeps this from becoming a talent directory."""
        self.rated(publish=True)
        payload = str(self.public())
        self.assertNotIn("Zainab Bello", payload)
        self.assertNotIn("Ngozi Adeyemi", payload)

    def test_it_can_be_narrowed_to_one_discipline(self):
        """So a service page shows reviews of that service."""
        self.rated(publish=True)
        self.assertEqual(len(self.public(product_line="design-creative")["reviews"]), 1)
        self.assertEqual(len(self.public(product_line="software-web")["reviews"]), 0)

    # --- the average has to be honest ---
    def test_no_average_below_the_sample_threshold(self):
        for _ in range(reviews_service.MIN_SAMPLE - 1):
            self.rated(publish=True)
        self.assertEqual(self.public()["summary"], {})

    def test_the_average_counts_every_review_not_just_the_published_ones(self):
        """Cherry-picking the quotes is expected. Cherry-picking the average
        would be a lie."""
        for _ in range(reviews_service.MIN_SAMPLE):
            self.rated(rating=5, publish=True)
        for _ in range(reviews_service.MIN_SAMPLE):
            self.rated(rating=1, publish=False)

        summary = self.public()["summary"]
        self.assertEqual(summary["count"], reviews_service.MIN_SAMPLE * 2)
        self.assertEqual(summary["average"], 3.0)

    # --- who can read it ---
    def test_a_signed_out_visitor_can_read_it(self):
        """It's the landing page's social proof — they haven't signed in yet."""
        self.rated(publish=True)
        response = APIClient().get("/api/reviews")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["reviews"]), 1)

    def test_the_private_boundary_is_unchanged(self):
        """Consenting to a public quote doesn't open the project's own feedback
        to the people who delivered it."""
        entry = self.rated(publish=True)
        detail = as_user(self.expert).get(
            f"/api/projects/{entry.project_id}").data
        self.assertIsNone(detail["feedback"])


class BoardFeedbackTests(TestCase):
    """A lead's own view of what their clients said."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "bflead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.other_lead = User.objects.create_user(
            "bfother@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.other_lead.product_lines.add(self.line)
        self.buyer = User.objects.create_user(
            "bfbuyer@acme.io", "x", role=User.Role.CLIENT)

    def rated(self, lead, rating=4, publish=False):
        project = Project.objects.create(
            title="A rebrand", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line, lead=lead,
            stage=Project.Stage.COMPLETED, quote_usd=1000,
            completed_at=timezone.now())
        return ProjectFeedback.objects.create(
            project=project, rating=rating, comment="Solid.",
            may_publish=publish)

    def stats(self, user):
        return as_user(user).get("/api/projects/stats/admin").data["feedback"]

    def test_a_lead_sees_their_own_including_unconsented(self):
        """This is their private signal, not the public wall."""
        self.rated(self.lead, publish=False)
        data = self.stats(self.lead)
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["recent"]), 1)

    def test_they_do_not_see_another_leads(self):
        self.rated(self.other_lead)
        self.assertEqual(self.stats(self.lead)["count"], 0)

    def test_an_unrated_lead_reads_null_not_zero(self):
        data = self.stats(self.lead)
        self.assertIsNone(data["average"])
        self.assertEqual(data["count"], 0)

    def test_the_average_is_their_own(self):
        self.rated(self.lead, rating=5)
        self.rated(self.lead, rating=3)
        self.rated(self.other_lead, rating=1)
        self.assertEqual(self.stats(self.lead)["average"], 4.0)

    def test_it_flags_which_ones_are_cleared_to_quote(self):
        self.rated(self.lead, publish=True)
        self.assertTrue(self.stats(self.lead)["recent"][0]["may_publish"])
