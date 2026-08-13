"""Moving a task between people, and never doing it silently.

Two things were missing. A task already handed in couldn't be reassigned at
all, so an expert going quiet mid-review left the work stuck with the one
person who couldn't finish it. And every edit and deletion was silent — no feed
entry, no email — so an expert could find their task re-scoped, re-priced or
gone and learn about it only by looking.

Editing and deleting stay the delivery lead's alone. What changes is that both
now put the reason on the record and tell the people it lands on.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments.models import Earning
from projects.models import Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def recipients():
    return {addr for m in mail.outbox for addr in m.to}


class TaskHandoverTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "holead@ril.team", "x", full_name="A Lead", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.ada = User.objects.create_user(
            "ada@ril.dev", "x", full_name="Ada Eze", role=User.Role.EXPERT)
        self.chidi = User.objects.create_user(
            "chidi@ril.dev", "x", full_name="Chidi Okonkwo", role=User.Role.EXPERT)
        for e in (self.ada, self.chidi):
            e.product_lines.add(self.line)
        self.bench = User.objects.create_user(
            "bench@ril.dev", "x", full_name="On Bench", role=User.Role.EXPERT)
        self.bench.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "hoclient@acme.io", "x", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.ada, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.ada, self.chidi)
        self.task = Task.objects.create(
            project=self.project, title="Visual design", assignee=self.ada,
            amount_usd=Decimal("1200.00"))
        mail.outbox = []

    def reassign(self, to=None, user=None, **extra):
        payload = {"assignee": to.id if to else None, **extra}
        return as_user(user or self.lead).post(
            f"/api/tasks/{self.task.id}/reassign", payload, format="json")

    def edit(self, user=None, **payload):
        return as_user(user or self.lead).patch(
            f"/api/tasks/{self.task.id}", payload, format="json")

    def remove(self, user=None, **payload):
        return as_user(user or self.lead).delete(
            f"/api/tasks/{self.task.id}", payload, format="json")

    # --- reassignment ---
    def test_a_lead_moves_a_task_to_someone_else_on_the_team(self):
        self.assertEqual(self.reassign(self.chidi).status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.chidi.id)

    def test_a_lead_unassigns_a_task(self):
        self.assertEqual(self.reassign(None).status_code, 200)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assignee_id)

    def test_an_unassigned_task_can_be_picked_up_again(self):
        self.reassign(None)
        self.assertEqual(self.reassign(self.chidi).status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.chidi.id)

    def test_a_submitted_task_can_still_be_moved(self):
        """The case that was stuck: handed in, and the holder has gone quiet."""
        as_user(self.ada).post(f"/api/tasks/{self.task.id}/submit")
        self.assertEqual(self.reassign(self.chidi).status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.chidi.id)

    def test_moving_a_submitted_task_takes_back_the_submission(self):
        """The hand-in belonged to whoever made it — the new holder hasn't
        submitted anything, and the lead shouldn't be able to approve as if
        they had."""
        as_user(self.ada).post(f"/api/tasks/{self.task.id}/submit")
        self.reassign(self.chidi)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.TODO)
        self.assertIsNone(self.task.submitted_at)
        self.assertEqual(
            as_user(self.lead).post(f"/api/tasks/{self.task.id}/approve").status_code,
            400)

    def test_an_approved_task_does_not_move(self):
        as_user(self.ada).post(f"/api/tasks/{self.task.id}/submit")
        as_user(self.lead).post(f"/api/tasks/{self.task.id}/approve")
        response = self.reassign(self.chidi)
        self.assertEqual(response.status_code, 400)
        self.assertIn("paid", str(response.data))
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.ada.id)

    def test_a_task_cannot_go_to_someone_off_the_team(self):
        self.assertEqual(self.reassign(self.bench).status_code, 400)

    def test_only_the_lead_moves_tasks(self):
        for someone in (self.ada, self.chidi, self.customer):
            self.assertEqual(self.reassign(self.chidi, user=someone).status_code, 403)

    def test_both_people_are_told(self):
        self.reassign(self.chidi, note="Ada is on leave")
        got = recipients()
        self.assertIn(self.ada.email, got, "the person losing it")
        self.assertIn(self.chidi.email, got, "the person gaining it")
        self.assertNotIn(self.customer.email, got, "not the client's business")

    def test_the_note_travels_with_it(self):
        self.reassign(self.chidi, note="Ada is on leave")
        bodies = " ".join(m.body for m in mail.outbox)
        self.assertIn("Ada is on leave", bodies)
        self.assertTrue(self.project.activity.filter(
            text__contains="Ada is on leave").exists())

    def test_the_move_lands_on_the_project_record(self):
        self.reassign(self.chidi)
        self.assertTrue(self.project.activity.filter(
            text__contains="from Ada Eze to Chidi Okonkwo").exists())

    def test_reassigning_to_the_same_person_changes_nothing(self):
        self.reassign(self.ada)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(self.project.activity.filter(text__contains="Moved").exists())

    # --- editing is the lead's, and it speaks ---
    def test_only_the_lead_edits(self):
        for someone in (self.ada, self.customer):
            self.assertEqual(self.edit(user=someone, title="Mine").status_code, 403)

    def test_an_edit_is_recorded_and_the_holder_is_told(self):
        response = self.edit(title="Visual design v2", amount_usd="900.00")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.ada.email, recipients())
        entry = self.project.activity.filter(text__contains="Edited").first()
        self.assertIsNotNone(entry)
        self.assertIn("$1,200.00 to $900.00", entry.text)

    def test_the_client_is_not_emailed_about_what_an_expert_earns(self):
        self.edit(amount_usd="900.00")
        self.assertNotIn(self.customer.email, recipients())

    def test_an_edit_that_changes_nothing_says_nothing(self):
        self.edit(title="Visual design")
        self.assertEqual(len(mail.outbox), 0)

    def test_the_assignee_cannot_be_changed_through_the_edit_endpoint(self):
        """One door for moving work, with one set of rules."""
        self.edit(assignee=self.chidi.id)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.ada.id)

    # --- deletion needs a reason ---
    def test_removing_a_task_requires_a_reason(self):
        response = self.remove(reason="   ")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_a_removal_tells_the_client_and_the_holder(self):
        response = self.remove(reason="Client dropped the animation")
        self.assertEqual(response.status_code, 204)
        got = recipients()
        self.assertIn(self.ada.email, got, "it was their work and their money")
        self.assertIn(self.customer.email, got, "it changes what they're getting")
        bodies = " ".join(m.body for m in mail.outbox)
        self.assertIn("Client dropped the animation", bodies)

    def test_the_reason_goes_on_the_project_record(self):
        self.remove(reason="Client dropped the animation")
        self.assertTrue(self.project.activity.filter(
            text__contains="Client dropped the animation").exists())

    def test_only_the_lead_removes(self):
        for someone in (self.ada, self.customer):
            self.assertEqual(self.remove(user=someone, reason="no").status_code, 403)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_a_paid_task_still_cannot_be_removed(self):
        Earning.objects.create(
            project=self.project, user=self.ada, task=self.task,
            kind=Earning.Kind.EXPERT, share_percent=Decimal("24.00"),
            amount_usd=Decimal("1200.00"))
        response = self.remove(reason="changed my mind")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_the_freed_amount_returns_to_the_pool(self):
        """A removed task stops committing its share, so the lead can spend it
        somewhere else."""
        before = self.project.unallocated_usd
        self.remove(reason="Not needed")
        self.project.refresh_from_db()
        self.assertEqual(self.project.unallocated_usd,
                         before + Decimal("1200.00"))
