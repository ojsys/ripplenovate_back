"""Project scope: who can reach a brief they aren't on.

Pins a real hole. Four endpoints looked a project up by id and then authorised
on `role == DELIVERY_LEAD`, which is true for every lead on the platform (an
admin carries that role too, which is how it read as an admin check). A lead
running one discipline could download another discipline's confidential
documents, tick off its tasks, delete its deliverables, and read its invoice —
and so could a lead whose application was still sitting in review.

The negative cases matter most, but the positive ones are here too: the fix is
only correct if the people who *should* reach a project still can.
"""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from catalog.models import ProductLine
from projects.models import Attachment, Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@override_settings(MEDIA_ROOT="/tmp/ril-test-access")
class ProjectScopeTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.other_line = ProductLine.objects.exclude(slug="design-creative").first()

        self.lead = User.objects.create_user(
            "owner@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        # Runs the same discipline but isn't on this brief: still in scope, the
        # way their board already shows it to them.
        self.peer_lead = User.objects.create_user(
            "peer@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.peer_lead.product_lines.add(self.line)
        # Runs a different discipline, no tie to this brief: out of scope.
        self.foreign_lead = User.objects.create_user(
            "foreign@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.foreign_lead.product_lines.add(self.other_line)
        # In the right discipline, but their application is still in review.
        self.pending_lead = User.objects.create_user(
            "pending@ril.team", "x", role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING)
        self.pending_lead.product_lines.add(self.line)

        self.admin = User.objects.create_superuser("admin@ril.team", "x")

        self.expert = User.objects.create_user(
            "exp@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.foreign_expert = User.objects.create_user(
            "otherexp@ril.dev", "x", role=User.Role.EXPERT)
        self.bizdev = User.objects.create_user(
            "bd@ril.team", "x", role=User.Role.BUSINESS_DEV)
        self.customer = User.objects.create_user(
            "client@acme.io", "x", role=User.Role.CLIENT)
        self.nosy_client = User.objects.create_user(
            "nosy@acme.io", "x", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="Confidential rebrand", client=self.customer,
            category="Brand identity", description="budgets and unreleased plans",
            product_line=self.line, lead=self.lead, expert=self.expert,
            business_developer=self.bizdev, stage=Project.Stage.IN_PROGRESS,
            quote_usd=2000)
        self.task = Task.objects.create(
            project=self.project, title="Draft the logo", assignee=self.expert,
            order=0)
        self.document = Attachment.objects.create(
            project=self.project,
            file=SimpleUploadedFile("brief.pdf", b"x" * 64,
                                    content_type="application/pdf"),
            original_filename="brief.pdf", size_bytes=64,
            purpose=Attachment.Purpose.DELIVERABLE, added_by=self.expert)

    def a_link(self):
        """A fresh link each time — deleting one is the point of the test."""
        return Attachment.objects.create(
            project=self.project, url="https://example.com/work",
            label="A deliverable", purpose=Attachment.Purpose.DELIVERABLE,
            added_by=self.expert)

    # --- requests, one per endpoint ---
    def download(self, user):
        return as_user(user).get(f"/api/attachments/{self.document.id}/download")

    def toggle(self, user):
        return as_user(user).patch(f"/api/tasks/{self.task.id}/toggle")

    def delete_link(self, user):
        return as_user(user).delete(f"/api/attachments/{self.a_link().id}")

    def invoice(self, user):
        return as_user(user).get(f"/api/projects/{self.project.id}/invoice")

    def detail(self, user):
        return as_user(user).get(f"/api/projects/{self.project.id}")

    # --- the hole ---
    def test_a_lead_from_another_discipline_is_shut_out(self):
        """The regression. Every one of these used to succeed."""
        self.assertEqual(self.download(self.foreign_lead).status_code, 403)
        self.assertEqual(self.toggle(self.foreign_lead).status_code, 403)
        self.assertEqual(self.delete_link(self.foreign_lead).status_code, 403)
        self.assertEqual(self.invoice(self.foreign_lead).status_code, 403)

    def test_a_foreign_lead_cannot_delete_someone_elses_deliverable(self):
        link = self.a_link()
        response = as_user(self.foreign_lead).delete(f"/api/attachments/{link.id}")
        self.assertEqual(response.status_code, 403)
        # The row — and the work it points at — is still there.
        self.assertTrue(Attachment.objects.filter(id=link.id).exists())

    def test_a_lead_still_in_review_cannot_act_on_the_work(self):
        """Approval gates acting on a project, not just quoting it."""
        self.assertEqual(self.toggle(self.pending_lead).status_code, 403)
        self.assertEqual(self.delete_link(self.pending_lead).status_code, 403)
        self.task.refresh_from_db()
        self.assertFalse(self.task.done)

    def test_experts_and_clients_off_the_project_are_shut_out(self):
        for outsider in (self.foreign_expert, self.nosy_client):
            self.assertEqual(self.download(outsider).status_code, 403)
            self.assertEqual(self.toggle(outsider).status_code, 403)
            self.assertEqual(self.invoice(outsider).status_code, 403)
            self.assertEqual(self.detail(outsider).status_code, 404)

    # --- and the people who should get through still do ---
    def test_the_delivery_team_and_client_keep_their_access(self):
        for insider in (self.lead, self.peer_lead, self.admin, self.expert,
                        self.customer):
            self.assertEqual(self.download(insider).status_code, 200,
                             f"{insider.email} lost document access")
            self.assertEqual(self.invoice(insider).status_code, 200,
                             f"{insider.email} lost invoice access")

    def test_the_business_developer_can_still_read_the_documents(self):
        self.assertEqual(self.download(self.bizdev).status_code, 200)

    def test_the_leads_who_run_the_line_can_still_work_the_project(self):
        for insider in (self.lead, self.peer_lead, self.admin, self.expert):
            self.assertEqual(self.toggle(insider).status_code, 200,
                             f"{insider.email} lost task access")
            self.assertEqual(self.delete_link(insider).status_code, 204,
                             f"{insider.email} lost attachment access")

    def test_whoever_added_a_link_can_still_remove_it(self):
        link = Attachment.objects.create(
            project=self.project, url="https://example.com/mine",
            label="Mine", purpose=Attachment.Purpose.REFERENCE,
            added_by=self.customer)
        response = as_user(self.customer).delete(f"/api/attachments/{link.id}")
        self.assertEqual(response.status_code, 204)

    def test_an_admin_still_sees_every_project(self):
        """The behaviour that made the bug hard to spot: for an admin, nothing
        changes. The role check they were exercising was simply also true for
        everyone else wearing the delivery_lead role."""
        response = as_user(self.admin).get("/api/projects")
        ids = [row["id"] for row in response.data]
        self.assertIn(self.project.id, ids)
        self.assertEqual(self.detail(self.admin).status_code, 200)

    def test_a_lead_still_in_review_cannot_move_the_work_along(self):
        """`_is_lead` gated the lifecycle actions on role alone, so a self-serve
        signup nobody had reviewed could hand work to a client, sign it off on
        their behalf, and release the earnings that follow — all on a board they
        were only ever meant to be watching."""
        client = as_user(self.pending_lead)
        base = f"/api/projects/{self.project.id}"

        self.assertEqual(client.post(f"{base}/submit-review").status_code, 403)
        self.assertEqual(client.post(f"{base}/remind-review").status_code, 403)
        self.assertEqual(
            client.post(f"{base}/attachments",
                        {"url": "https://figma.com/file/x"}, format="json").status_code,
            403)

        # And the one that moves money: approval credits every share.
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        self.assertEqual(client.post(f"{base}/approve").status_code, 403)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.REVIEW)

    def test_an_approved_lead_still_runs_their_own_board(self):
        """The other half: approval gates the actions, it doesn't remove them."""
        client = as_user(self.lead)
        base = f"/api/projects/{self.project.id}"
        self.assertEqual(client.post(f"{base}/submit-review").status_code, 200)
        self.assertEqual(client.post(f"{base}/remind-review").status_code, 200)
        self.assertEqual(client.post(f"{base}/approve").status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.COMPLETED)

    def test_a_project_with_no_product_line_stays_private_to_its_own_team(self):
        """No line means no discipline to inherit access from — only the people
        named on the brief get in. Guards against `product_line_id = None`
        matching a lead who also has no lines."""
        loose = Project.objects.create(
            title="Unfiled brief", client=self.customer, category="Other",
            description="…", lead=self.lead, stage=Project.Stage.SUBMITTED,
            quote_usd=500)
        lineless_lead = User.objects.create_user(
            "nolines@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        doc = Attachment.objects.create(
            project=loose, url="https://example.com/loose", label="x",
            purpose=Attachment.Purpose.REFERENCE, added_by=self.customer)
        self.assertEqual(
            as_user(lineless_lead).delete(f"/api/attachments/{doc.id}").status_code,
            403)
        self.assertEqual(
            as_user(lineless_lead).get(f"/api/projects/{loose.id}/invoice").status_code,
            403)
