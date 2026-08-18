"""Closing a project over the client's head (G3).

A lead could complete any project in Review and release every earning on it,
including their own 15%. There was a real reason for the power — a client who
stops replying must not be able to strand the team's money — but as written it
also let a lead take payment for work the client had just rejected.

These tests pin the three conditions that now apply to that path, and pin that
none of them touch the client's own right to approve.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SiteSettings
from catalog.models import ProductLine
from payments.models import Earning
from projects.models import Activity, Project

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class CompletionGuardTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "guardlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.peer = User.objects.create_user(
            "guardpeer@ril.team", "x", full_name="Another Lead",
            role=User.Role.DELIVERY_LEAD)
        self.peer.product_lines.add(self.line)
        self.admin = User.objects.create_superuser(
            "guardboss@ril.team", "x", full_name="An Admin")
        self.expert = User.objects.create_user(
            "guardexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "guardclient@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.REVIEW, quote_usd=4000)
        self.project.experts.add(self.expert)

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    def reload(self):
        self.project.refresh_from_db()
        return self.project

    def remind(self, days_ago=0):
        as_user(self.lead).post(self.url("/remind-review"))
        if days_ago:
            self.project.refresh_from_db()
            self.project.review_reminded_at = timezone.now() - timedelta(days=days_ago)
            self.project.save(update_fields=["review_reminded_at"])

    # --- the client is never constrained ---
    def test_the_client_can_always_approve_immediately(self):
        response = as_user(self.customer).post(self.url("/approve"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.reload().stage, Project.Stage.COMPLETED)
        self.assertEqual(self.reload().completed_by_id, self.customer.id)
        self.assertIsNone(self.reload().countersigned_by_id)

    # --- the lead must ask first ---
    def test_a_lead_cannot_complete_without_reminding(self):
        response = as_user(self.lead).post(self.url("/approve"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("Remind the client first", str(response.data))
        self.assertEqual(self.reload().stage, Project.Stage.REVIEW)

    def test_a_lead_cannot_complete_the_moment_they_remind(self):
        self.remind()
        response = as_user(self.lead).post(self.url("/approve"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reload().stage, Project.Stage.REVIEW)

    def test_a_lead_can_complete_once_the_client_has_gone_quiet(self):
        self.remind(days_ago=8)
        response = as_user(self.lead).post(self.url("/approve"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.reload().stage, Project.Stage.COMPLETED)
        self.assertEqual(self.reload().completed_by_id, self.lead.id)

    def test_the_window_is_configurable(self):
        settings_row = SiteSettings.load()
        settings_row.client_silence_days = 30
        settings_row.save(update_fields=["client_silence_days"])
        self.remind(days_ago=8)
        self.assertEqual(as_user(self.lead).post(self.url("/approve")).status_code, 403)

    def test_reminding_again_does_not_restart_the_clock(self):
        """Otherwise chasing a client would postpone the team's own payday."""
        self.remind(days_ago=8)
        first = self.reload().review_reminded_at
        as_user(self.lead).post(self.url("/remind-review"))
        self.assertEqual(self.reload().review_reminded_at, first)
        self.assertEqual(as_user(self.lead).post(self.url("/approve")).status_code, 200)

    # --- a client who is talking is not silent ---
    def test_a_client_who_replies_resets_the_clock(self):
        self.remind(days_ago=8)
        as_user(self.customer).post(
            self.url("/activity"), {"kind": "question", "text": "Can we see it in blue?"},
            format="json")
        response = as_user(self.lead).post(self.url("/approve"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("been in touch", str(response.data))

    def test_the_team_talking_among_themselves_does_not_count(self):
        self.remind(days_ago=8)
        as_user(self.lead).post(
            self.url("/activity"), {"kind": "progress", "text": "Chased again."},
            format="json")
        self.assertEqual(as_user(self.lead).post(self.url("/approve")).status_code, 200)

    # --- an open change request blocks everything ---
    def test_an_open_revision_blocks_completion(self):
        """The worst version of this: taking payment for work they rejected."""
        self.remind(days_ago=30)
        as_user(self.customer).post(
            self.url("/request-changes"), {"note": "Wrong colours."}, format="json")
        # Back to In Progress, so put it in Review again the way the team would.
        as_user(self.expert).post(self.url("/submit-review"))
        # Re-open a request on the resubmitted work.
        as_user(self.customer).post(
            self.url("/request-changes"), {"note": "Still wrong."}, format="json")
        self.project.refresh_from_db()
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])

        response = as_user(self.lead).post(self.url("/approve"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("asked for changes", str(response.data))

    # --- the countersignature route ---
    # Administrators, not peer leads. `access.visible_projects` deliberately
    # hides a lead's project from every other lead, so a peer can't even fetch
    # it — and widening that boundary to enable this would trade a client's
    # budget privacy for a convenience.
    def test_an_admin_can_countersign_without_the_wait(self):
        self.remind()
        response = as_user(self.admin).post(self.url("/countersign-completion"))
        self.assertEqual(response.status_code, 200, response.data)
        project = self.reload()
        self.assertEqual(project.stage, Project.Stage.COMPLETED)
        self.assertEqual(project.completed_by_id, self.lead.id,
                         "the lead is still who completed it")
        self.assertEqual(project.countersigned_by_id, self.admin.id)

    def test_countersigning_needs_no_reminder_at_all(self):
        self.assertEqual(
            as_user(self.admin).post(self.url("/countersign-completion")).status_code, 200)

    def test_a_peer_lead_cannot_countersign(self):
        """They can't see the project at all, which is the correct answer —
        pinned here so a future visibility change has to think about this."""
        self.assertIn(
            as_user(self.peer).post(self.url("/countersign-completion")).status_code,
            (403, 404))

    def test_the_owning_lead_cannot_countersign_their_own_project(self):
        self.assertEqual(
            as_user(self.lead).post(self.url("/countersign-completion")).status_code, 403)

    def test_a_countersignature_cannot_override_the_client(self):
        as_user(self.customer).post(
            self.url("/request-changes"), {"note": "No."}, format="json")
        self.project.refresh_from_db()
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        response = as_user(self.admin).post(self.url("/countersign-completion"))
        self.assertEqual(response.status_code, 403)

    def test_an_expert_cannot_countersign(self):
        self.assertIn(
            as_user(self.expert).post(self.url("/countersign-completion")).status_code,
            (403, 404))

    def test_the_client_cannot_countersign_their_way_around_anything(self):
        self.assertEqual(
            as_user(self.customer).post(self.url("/countersign-completion")).status_code, 403)

    # --- money still moves correctly on both paths ---
    def test_earnings_are_released_on_the_silence_path(self):
        self.remind(days_ago=8)
        as_user(self.lead).post(self.url("/approve"))
        self.assertTrue(
            Earning.objects.filter(project=self.project, user=self.lead).exists())

    def test_earnings_are_released_on_the_countersign_path(self):
        self.remind()
        as_user(self.admin).post(self.url("/countersign-completion"))
        self.assertTrue(
            Earning.objects.filter(project=self.project, user=self.lead).exists())

    def test_the_countersignature_is_named_in_the_feed(self):
        self.remind()
        as_user(self.admin).post(self.url("/countersign-completion"))
        text = self.project.activity.latest("id").text
        self.assertIn("countersigned by", text.lower())
        self.assertIn("An Admin", text)

    # --- what the UI is told ---
    def test_the_block_reason_is_exposed_before_they_click(self):
        detail = as_user(self.lead).get(self.url()).data
        self.assertIn("Remind the client first", detail["completion_block"])
        self.remind(days_ago=8)
        detail = as_user(self.lead).get(self.url()).data
        self.assertIsNone(detail["completion_block"])

    def test_no_block_is_reported_outside_review(self):
        self.project.stage = Project.Stage.IN_PROGRESS
        self.project.save(update_fields=["stage"])
        detail = as_user(self.lead).get(self.url()).data
        self.assertIsNone(detail["completion_block"])


class AdminCompletionTests(TestCase):
    """A superuser is exempt from the peer rule elsewhere in the codebase
    (settling their own withdrawal), so pin what happens here."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.admin = User.objects.create_superuser("gboss@ril.team", "x")
        self.admin.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "gadminclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="Admin case", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.admin,
            stage=Project.Stage.REVIEW, quote_usd=1000)

    def test_an_admin_is_still_held_to_the_silence_rule(self):
        """Deliberate. The rule protects the client, not the platform's
        hierarchy — and an admin who genuinely needs to override can still edit
        the row in the Django admin, under their own name."""
        response = as_user(self.admin).post(
            f"/api/projects/{self.project.id}/approve")
        self.assertEqual(response.status_code, 403)
