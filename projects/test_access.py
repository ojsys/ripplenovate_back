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
        # Runs the same discipline, but this brief is somebody else's: out of
        # scope. Sharing a discipline is not a claim on another lead's client.
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

    def edit_task(self, user):
        """Maintaining the task list is the running lead's job."""
        return as_user(user).patch(f"/api/tasks/{self.task.id}",
                                   {"title": "Renamed"}, format="json")

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
        self.assertEqual(self.edit_task(self.foreign_lead).status_code, 403)
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
        self.assertEqual(self.edit_task(self.pending_lead).status_code, 403)
        self.assertEqual(self.delete_link(self.pending_lead).status_code, 403)
        self.task.refresh_from_db()
        self.assertFalse(self.task.done)

    def test_experts_and_clients_off_the_project_are_shut_out(self):
        for outsider in (self.foreign_expert, self.nosy_client):
            self.assertEqual(self.download(outsider).status_code, 403)
            self.assertEqual(self.edit_task(outsider).status_code, 403)
            self.assertEqual(self.invoice(outsider).status_code, 403)
            self.assertEqual(self.detail(outsider).status_code, 404)

    def test_another_lead_in_the_same_discipline_is_shut_out_too(self):
        """The stricter half. Running the same discipline as someone is not a
        claim on their client's brief — once a lead has quoted it, it's theirs
        and it leaves every other board."""
        self.assertEqual(self.detail(self.peer_lead).status_code, 404)
        self.assertEqual(self.download(self.peer_lead).status_code, 403)
        self.assertEqual(self.edit_task(self.peer_lead).status_code, 403)
        self.assertEqual(self.delete_link(self.peer_lead).status_code, 403)
        self.assertEqual(self.invoice(self.peer_lead).status_code, 403)
        self.assertEqual(
            as_user(self.peer_lead).post(
                f"/api/projects/{self.project.id}/activity",
                {"text": "Butting in", "kind": "note"}, format="json").status_code,
            404)
        self.assertEqual(
            as_user(self.peer_lead).patch(
                f"/api/projects/{self.project.id}/edit",
                {"title": "Renamed"}, format="json").status_code,
            404)
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "Confidential rebrand")

    # --- and the people who should get through still do ---
    def test_the_delivery_team_and_client_keep_their_access(self):
        for insider in (self.lead, self.admin, self.expert, self.customer):
            self.assertEqual(self.download(insider).status_code, 200,
                             f"{insider.email} lost document access")
            self.assertEqual(self.invoice(insider).status_code, 200,
                             f"{insider.email} lost invoice access")

    def test_the_business_developer_can_still_read_the_documents(self):
        self.assertEqual(self.download(self.bizdev).status_code, 200)

    def test_the_lead_who_runs_it_can_still_work_the_project(self):
        for insider in (self.lead, self.admin):
            self.assertEqual(self.edit_task(insider).status_code, 200,
                             f"{insider.email} lost task access")
        # The expert delivers the work; the lead maintains the list. An expert
        # re-pricing their own task would be setting their own fee.
        self.assertEqual(self.edit_task(self.expert).status_code, 403)
        for insider in (self.lead, self.admin, self.expert):
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
        # On a project of their own, so it's the approval gate under test here
        # and not the scoping one — those are checked separately above.
        own = Project.objects.create(
            title="Their own brief", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.pending_lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        client = as_user(self.pending_lead)
        base = f"/api/projects/{own.id}"
        self.assertEqual(client.get(base).status_code, 200, "should still see it")

        self.assertEqual(client.post(f"{base}/submit-review").status_code, 403)
        self.assertEqual(client.post(f"{base}/remind-review").status_code, 403)
        self.assertEqual(
            client.post(f"{base}/attachments",
                        {"url": "https://figma.com/file/x"}, format="json").status_code,
            403)

        # And the one that moves money: approval credits every share.
        own.stage = Project.Stage.REVIEW
        own.save(update_fields=["stage"])
        self.assertEqual(client.post(f"{base}/approve").status_code, 403)
        own.refresh_from_db()
        self.assertEqual(own.stage, Project.Stage.REVIEW)

    def test_an_approved_lead_still_runs_their_own_board(self):
        """The other half: approval gates the actions, it doesn't remove them."""
        client = as_user(self.lead)
        base = f"/api/projects/{self.project.id}"
        self.assertEqual(client.post(f"{base}/submit-review").status_code, 200)
        self.assertEqual(client.post(f"{base}/remind-review").status_code, 200)
        self.assertEqual(client.post(f"{base}/approve").status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.COMPLETED)

    def unclaimed(self):
        """A fresh brief nobody has quoted — `lead` is unset until someone does."""
        return Project.objects.create(
            title="Unclaimed brief", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, stage=Project.Stage.SUBMITTED)

    def test_an_unquoted_brief_reaches_every_lead_who_runs_that_line(self):
        """The intake queue. `lead` is only set when someone quotes, so scoping
        strictly to `lead=user` would hide new briefs from everyone and nothing
        could ever be picked up."""
        brief = self.unclaimed()
        for lead in (self.lead, self.peer_lead):
            codes = {p["code"] for p in as_user(lead).get("/api/projects").data}
            self.assertIn(brief.code, codes, lead.email)
            self.assertEqual(
                as_user(lead).get(f"/api/projects/{brief.id}").status_code, 200)

    def test_quoting_claims_the_brief_and_clears_it_from_other_boards(self):
        """The handover the whole rule turns on."""
        brief = self.unclaimed()
        response = as_user(self.peer_lead).post(
            f"/api/projects/{brief.id}/quote", {"quote_usd": 1500}, format="json")
        self.assertEqual(response.status_code, 200)
        brief.refresh_from_db()
        self.assertEqual(brief.lead_id, self.peer_lead.id)

        # It's the quoting lead's now — and nobody else's, same discipline or not.
        codes = {p["code"] for p in as_user(self.lead).get("/api/projects").data}
        self.assertNotIn(brief.code, codes)
        self.assertEqual(
            as_user(self.lead).get(f"/api/projects/{brief.id}").status_code, 404)
        self.assertEqual(
            as_user(self.peer_lead).get(f"/api/projects/{brief.id}").status_code, 200)

    def test_an_unquoted_brief_stays_within_its_own_discipline(self):
        brief = self.unclaimed()
        self.assertEqual(
            as_user(self.foreign_lead).get(f"/api/projects/{brief.id}").status_code, 404)

    def test_the_board_stats_agree_with_the_board(self):
        """`visible_projects` feeds the stat tiles; if it drifts from the
        queryset the numbers describe projects the lead can't open."""
        self.unclaimed()
        for lead in (self.lead, self.peer_lead):
            listed = {p["id"] for p in as_user(lead).get("/api/projects").data}
            stats = as_user(lead).get("/api/projects/stats/admin").data
            active = {p for p in listed
                      if Project.objects.get(id=p).stage != Project.Stage.COMPLETED}
            self.assertEqual(stats["active_total"], len(active), lead.email)

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
