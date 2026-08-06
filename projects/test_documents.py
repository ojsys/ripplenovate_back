"""Project document uploads, and the regressions found in testing.

Three of these pin bugs a tester hit: the payout form that never rendered during
onboarding, and the two ways an approved partner could get trapped in the signup
wizard.
"""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from catalog.models import ProductLine
from projects.models import Attachment, Project

User = get_user_model()


def a_file(name="brief.pdf", size=512):
    return SimpleUploadedFile(name, b"x" * size, content_type="application/pdf")


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@override_settings(MEDIA_ROOT="/tmp/ril-test-docs")
class ProjectDocumentTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "doclead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "docexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.customer = User.objects.create_user(
            "docclient@acme.io", "x", role=User.Role.CLIENT)
        self.outsider = User.objects.create_user(
            "nosy@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=2000)

    def upload(self, user, **kw):
        return as_user(user).post(
            f"/api/projects/{self.project.id}/documents",
            {"file": a_file(**kw)}, format="multipart")

    def test_a_client_uploads_a_brief_document(self):
        response = self.upload(self.customer, name="Project-brief.pdf")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["kind"], "file")
        self.assertTrue(response.data["is_file"])
        self.assertEqual(response.data["original_filename"], "Project-brief.pdf")
        # A client supplies references, never deliverables.
        self.assertEqual(response.data["purpose"], "reference")

    def test_a_client_can_hand_over_a_brief_either_way(self):
        """A brief arrives as a file as often as it arrives as a Drive link, and
        it can occur to a client after they've submitted. Both routes stay open
        on a live project, and both are filed as references."""
        uploaded = self.upload(self.customer, name="Scope.docx")
        linked = as_user(self.customer).post(
            f"/api/projects/{self.project.id}/attachments",
            {"url": "https://docs.google.com/document/d/1", "label": "The brief"},
            format="json")

        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(linked.status_code, 201)
        self.assertEqual(
            {a["purpose"] for a in (uploaded.data, linked.data)}, {"reference"})
        self.assertEqual(
            {a["kind"] for a in (uploaded.data, linked.data)}, {"file", "drive"})

        # Both show up on the brief, for the client and the delivery team alike.
        for viewer in (self.customer, self.lead, self.expert):
            detail = as_user(viewer).get(f"/api/projects/{self.project.id}").data
            refs = [a for a in detail["attachments"] if a["purpose"] == "reference"]
            self.assertEqual(len(refs), 2, viewer.email)

    def test_a_client_can_add_a_reference_at_any_stage(self):
        """Including after payment and once the work is in review — a missing
        asset is exactly the thing that surfaces late."""
        for stage in (Project.Stage.SUBMITTED, Project.Stage.QUOTED,
                      Project.Stage.PAID, Project.Stage.REVIEW,
                      Project.Stage.COMPLETED):
            self.project.stage = stage
            self.project.save(update_fields=["stage"])
            self.assertEqual(self.upload(self.customer).status_code, 201, stage)
            response = as_user(self.customer).post(
                f"/api/projects/{self.project.id}/attachments",
                {"url": f"https://example.com/{stage}"}, format="json")
            self.assertEqual(response.status_code, 201, stage)

    def test_the_expert_and_lead_can_upload_deliverables(self):
        for user in (self.expert, self.lead):
            response = self.upload(user)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data["purpose"], "deliverable")

    def test_an_outsider_cannot_upload(self):
        self.assertEqual(self.upload(self.outsider).status_code, 404)

    def test_an_executable_is_refused(self):
        response = self.upload(self.customer, name="payload.exe")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.project.attachments.count(), 0)

    def test_a_file_over_5mb_is_refused(self):
        response = self.upload(self.customer, size=6 * 1024 * 1024)
        self.assertEqual(response.status_code, 400)

    def test_the_stored_name_does_not_leak_the_original(self):
        self.upload(self.customer, name="Confidential-Acme-Budget.pdf")
        stored = Attachment.objects.get(project=self.project).file.name
        self.assertNotIn("Confidential", stored)
        self.assertTrue(stored.startswith("projects/"))

    def test_everyone_on_the_project_can_download(self):
        doc_id = self.upload(self.customer).data["id"]
        for user in (self.customer, self.expert, self.lead):
            response = as_user(user).get(f"/api/attachments/{doc_id}/download")
            self.assertEqual(response.status_code, 200, user.email)
            self.assertIn("no-store", response["Cache-Control"])

    def test_an_outsider_cannot_download(self):
        doc_id = self.upload(self.customer).data["id"]
        response = as_user(self.outsider).get(f"/api/attachments/{doc_id}/download")
        self.assertEqual(response.status_code, 403)

    def test_deleting_removes_the_file_too(self):
        import os
        doc_id = self.upload(self.expert).data["id"]
        path = Attachment.objects.get(id=doc_id).file.path
        self.assertTrue(os.path.exists(path))
        self.assertEqual(
            as_user(self.expert).delete(f"/api/attachments/{doc_id}").status_code, 204)
        self.assertFalse(os.path.exists(path))

    def test_uploads_and_links_live_side_by_side(self):
        self.upload(self.expert)
        as_user(self.expert).post(
            f"/api/projects/{self.project.id}/attachments",
            {"url": "https://figma.com/file/abc"}, format="json")
        detail = as_user(self.customer).get(f"/api/projects/{self.project.id}").data
        kinds = {a["kind"] for a in detail["attachments"]}
        self.assertEqual(kinds, {"file", "figma"})

    def test_an_attachment_cannot_be_both_a_link_and_a_file(self):
        from django.core.exceptions import ValidationError

        attachment = Attachment(
            project=self.project, url="https://example.com", file=a_file())
        with self.assertRaises(ValidationError):
            attachment.full_clean()


class OnboardingRegressionTests(TestCase):
    """Bugs a tester hit, pinned so they can't come back."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="software-web")
        self.pending = User.objects.create_user(
            "pendinglead@ril.team", "x", role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING)
        self.pending.product_lines.add(self.line)
        self.admin = User.objects.create_superuser("regadmin@ril.team", "x")

    def test_a_partner_in_review_can_set_up_their_payout_account(self):
        """The onboarding wizard's payout step 403'd, so it showed a spinner
        forever and people continued past it with no bank account on file."""
        client = as_user(self.pending)
        self.assertEqual(client.get("/api/payouts/account").status_code, 200)

    def test_a_partner_in_review_still_cannot_withdraw(self):
        """Setting up where you'd be paid is not the same as being paid."""
        self.assertEqual(as_user(self.pending).get("/api/earnings").status_code, 403)

    def test_approving_closes_onboarding_so_nobody_is_trapped(self):
        """An admin approving someone who never submitted used to leave
        onboarding 'incomplete', which bounced them back into the wizard."""
        self.assertIsNone(self.pending.onboarding_completed_at)
        self.pending.approve(by=self.admin)
        self.pending.refresh_from_db()
        self.assertIsNotNone(self.pending.onboarding_completed_at)
        self.assertTrue(self.pending.is_approved)

    def test_submitting_after_being_approved_succeeds_rather_than_erroring(self):
        """It used to answer 'your account is already approved' with a 400."""
        self.pending.approve(by=self.admin)
        User.objects.filter(pk=self.pending.pk).update(onboarding_completed_at=None)
        # Re-read: force_authenticate would otherwise hand the view the stale
        # in-memory object, and the test would pass for the wrong reason.
        stale_free = User.objects.get(pk=self.pending.pk)
        response = as_user(stale_free).post("/api/onboarding/submit")
        self.assertEqual(response.status_code, 200)
        self.pending.refresh_from_db()
        self.assertIsNotNone(self.pending.onboarding_completed_at)

    def test_a_partner_in_review_can_still_open_a_brief(self):
        """The router bounced them to onboarding from every route, so a link to
        a project was unreachable. The API always allowed it — this pins that."""
        customer = User.objects.create_user("c9@acme.io", "x", role=User.Role.CLIENT)
        project = Project.objects.create(
            title="A brief", client=customer, category="Web application",
            description="…", product_line=self.line)
        response = as_user(self.pending).get(f"/api/projects/{project.id}")
        self.assertEqual(response.status_code, 200)
