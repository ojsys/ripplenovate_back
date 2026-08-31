"""Sending delivered work back (G1).

Review used to have one exit. A client who wasn't happy could only decline to
click Approve, which told the team nothing and left the project parked in a
stage nobody could move it out of. These tests pin the second exit, and pin the
thing that makes it safe to have: money already released is never touched.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments.models import Earning
from projects.models import Activity, Project, RevisionRequest, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class RevisionTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "revlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "revexpert@ril.dev", "x", full_name="An Expert",
            role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "revclient@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)
        self.other_client = User.objects.create_user(
            "nosy@acme.io", "x", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.REVIEW, quote_usd=5000)
        self.project.experts.add(self.expert)

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    def send_back(self, note="The logo is the wrong colour.", by=None):
        return as_user(by or self.customer).post(
            self.url("/request-changes"), {"note": note}, format="json")

    def reload(self):
        self.project.refresh_from_db()
        return self.project

    # --- the transition ---
    def test_the_client_can_send_work_back(self):
        response = self.send_back()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.reload().stage, Project.Stage.IN_PROGRESS)

    def test_it_records_what_was_wrong(self):
        self.send_back("The logo is the wrong colour.")
        revision = RevisionRequest.objects.get()
        self.assertEqual(revision.project_id, self.project.id)
        self.assertEqual(revision.requested_by_id, self.customer.id)
        self.assertEqual(revision.note, "The logo is the wrong colour.")
        self.assertIsNone(revision.resolved_at)

    def test_a_reason_is_required(self):
        """"Send it back" with no reason is how a revision loop becomes an argument."""
        for note in ("", "   "):
            with self.subTest(note=repr(note)):
                self.assertEqual(self.send_back(note).status_code, 400)
        self.assertEqual(RevisionRequest.objects.count(), 0)
        self.assertEqual(self.reload().stage, Project.Stage.REVIEW)

    def test_the_round_counter_moves(self):
        self.send_back()
        self.assertEqual(self.reload().revision_rounds, 1)
        as_user(self.expert).post(self.url("/submit-review"))
        self.send_back("Still not right.")
        self.assertEqual(self.reload().revision_rounds, 2)

    def test_it_lands_in_the_feed_as_its_own_kind(self):
        self.send_back("Needs more contrast.")
        entry = self.project.activity.latest("id")
        self.assertEqual(entry.kind, Activity.Kind.REVISION)
        self.assertEqual(entry.text, "Needs more contrast.")

    # --- who may do it ---
    def test_only_this_project_s_client(self):
        for who in (self.lead, self.expert, self.other_client):
            with self.subTest(who=who.email):
                self.assertIn(self.send_back(by=who).status_code, (403, 404))
        self.assertEqual(self.reload().stage, Project.Stage.REVIEW)

    def test_an_admin_can_do_it_on_their_behalf(self):
        admin = User.objects.create_superuser("revboss@ril.team", "x")
        self.assertEqual(self.send_back(by=admin).status_code, 200)

    def test_it_only_works_from_review(self):
        for stage in (Project.Stage.SUBMITTED, Project.Stage.QUOTED,
                      Project.Stage.PAID, Project.Stage.IN_PROGRESS,
                      Project.Stage.COMPLETED):
            with self.subTest(stage=stage):
                self.project.stage = stage
                self.project.save(update_fields=["stage"])
                self.assertEqual(self.send_back().status_code, 400)

    # --- the money is not touched ---
    def test_approved_task_payments_survive_a_revision(self):
        """Rework is new work. Unwinding released payments would turn every
        revision into a payment dispute, and the cash was collected up front."""
        task = Task.objects.create(
            project=self.project, title="Logo concepts", assignee=self.expert,
            amount_usd=Decimal("900"), status=Task.Status.APPROVED)
        earning = Earning.objects.create(
            user=self.expert, project=self.project, task=task,
            kind=Earning.Kind.EXPERT, share_percent=Decimal("18"),
            amount_usd=Decimal("900"))

        self.send_back()

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.APPROVED)
        self.assertTrue(Earning.objects.filter(id=earning.id).exists())
        self.assertEqual(
            Earning.objects.get(id=earning.id).amount_usd, Decimal("900"))

    def test_no_earning_is_ever_negative(self):
        """The invariant the whole refund design rests on, pinned here because
        this is the first action that could plausibly have reversed one."""
        self.send_back()
        self.assertFalse(Earning.objects.filter(amount_usd__lt=0).exists())

    # --- closing the loop ---
    def test_resubmitting_resolves_the_open_request(self):
        self.send_back()
        as_user(self.expert).post(self.url("/submit-review"))
        revision = RevisionRequest.objects.get()
        self.assertIsNotNone(revision.resolved_at)
        self.assertEqual(self.reload().stage, Project.Stage.REVIEW)

    def test_open_revision_is_exposed_while_it_stands(self):
        self.send_back("Fix the kerning.")
        detail = as_user(self.customer).get(self.url()).data
        self.assertEqual(detail["open_revision"]["note"], "Fix the kerning.")
        self.assertEqual(detail["revision_rounds"], 1)

        as_user(self.expert).post(self.url("/submit-review"))
        detail = as_user(self.customer).get(self.url()).data
        self.assertIsNone(detail["open_revision"])
        self.assertEqual(len(detail["revision_history"]), 1)

    # --- finding it again afterwards ---
    def test_the_board_flags_a_project_the_client_sent_back(self):
        """The lead's board, not just the notification that announced it.

        A returned project sits at In Progress like any other, so the row read
        "View" in the same grey as work that was ticking along fine — once the
        notification was read there was nothing left pointing at it.
        """
        self.send_back()
        row = next(j for j in as_user(self.lead).get("/api/projects").data
                   if j["id"] == self.project.id)
        self.assertTrue(row["has_open_revision"])
        self.assertEqual(row["stage"], Project.Stage.IN_PROGRESS.value)

    def test_the_flag_clears_when_the_team_resubmits(self):
        self.send_back()
        as_user(self.expert).post(self.url("/submit-review"))
        row = next(j for j in as_user(self.lead).get("/api/projects").data
                   if j["id"] == self.project.id)
        self.assertFalse(row["has_open_revision"])
        # The counter stays up — it's the history. The flag is the queue.
        self.assertEqual(row["revision_rounds"], 1)

    def test_a_project_that_was_never_sent_back_is_not_flagged(self):
        row = next(j for j in as_user(self.lead).get("/api/projects").data
                   if j["id"] == self.project.id)
        self.assertFalse(row["has_open_revision"])

    def test_the_board_stat_counts_what_is_waiting(self):
        stats = as_user(self.lead).get("/api/projects/stats/admin").data
        self.assertEqual(stats["changes_requested"], 0)
        self.send_back()
        stats = as_user(self.lead).get("/api/projects/stats/admin").data
        self.assertEqual(stats["changes_requested"], 1)
        as_user(self.expert).post(self.url("/submit-review"))
        stats = as_user(self.lead).get("/api/projects/stats/admin").data
        self.assertEqual(stats["changes_requested"], 0)

    def test_a_second_round_counts_once_not_twice(self):
        """Two resolved rounds and one open is one project to work on."""
        self.send_back("Round one.")
        as_user(self.expert).post(self.url("/submit-review"))
        self.send_back("Round two.")
        stats = as_user(self.lead).get("/api/projects/stats/admin").data
        self.assertEqual(stats["changes_requested"], 1)
        row = next(j for j in as_user(self.lead).get("/api/projects").data
                   if j["id"] == self.project.id)
        self.assertTrue(row["has_open_revision"])
        self.assertEqual(row["revision_rounds"], 2)

    def test_the_client_can_still_approve_after_a_round(self):
        self.send_back()
        as_user(self.expert).post(self.url("/submit-review"))
        response = as_user(self.customer).post(self.url("/approve"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.reload().stage, Project.Stage.COMPLETED)

    # --- who hears about it ---
    def test_the_whole_delivery_team_is_told(self):
        mail.outbox = []
        self.send_back()
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.lead.email, recipients)
        self.assertIn(self.expert.email, recipients)

    def test_the_note_travels_with_the_notification(self):
        """So nobody has to open the app to find out how bad it is."""
        mail.outbox = []
        self.send_back("The typeface is wrong throughout.")
        body = " ".join(m.body for m in mail.outbox)
        self.assertIn("The typeface is wrong throughout.", body)

    def test_resubmission_tells_the_client_their_changes_were_made(self):
        self.send_back()
        mail.outbox = []
        as_user(self.expert).post(self.url("/submit-review"))
        to_client = [m for m in mail.outbox if self.customer.email in m.to]
        self.assertTrue(to_client, "the client was not told")
        self.assertIn("changes", " ".join(m.subject for m in to_client).lower())

    def test_a_first_submission_does_not_claim_changes_were_made(self):
        fresh = Project.objects.create(
            title="Another brief", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        fresh.experts.add(self.expert)
        mail.outbox = []
        as_user(self.expert).post(f"/api/projects/{fresh.id}/submit-review")
        subjects = " ".join(m.subject for m in mail.outbox).lower()
        self.assertIn("ready for your review", subjects)
