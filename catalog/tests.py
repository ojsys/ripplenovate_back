"""Product-line routing and scoping.

These are permission boundaries, not just filters: before product lines, every
delivery lead could see and quote every brief on the platform. Getting this
wrong exposes one line's client work to another line's leads.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine, Service
from projects.models import Project

User = get_user_model()


class ScopingTestCase(TestCase):
    def setUp(self):
        self.software = ProductLine.objects.get(slug="software-web")
        self.design = ProductLine.objects.get(slug="design-creative")

        self.software_lead = User.objects.create_user(
            "swlead@ril.team", "x", full_name="SW Lead", role=User.Role.DELIVERY_LEAD)
        self.software_lead.product_lines.add(self.software)

        self.design_lead = User.objects.create_user(
            "dlead@ril.team", "x", full_name="Design Lead", role=User.Role.DELIVERY_LEAD)
        self.design_lead.product_lines.add(self.design)

        self.customer = User.objects.create_user(
            "c@acme.io", "x", role=User.Role.CLIENT, company="Acme")

        self.sw_project = Project.objects.create(
            title="An app", client=self.customer, category="Web application",
            description="…", product_line=self.software)
        self.design_project = Project.objects.create(
            title="A logo", client=self.customer, category="Brand identity",
            description="…", product_line=self.design)

    def as_user(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client


class LeadScopingTests(ScopingTestCase):
    def test_a_lead_only_sees_briefs_in_their_own_lines(self):
        codes = {p["code"] for p in self.as_user(self.software_lead).get("/api/projects").data}
        self.assertIn(self.sw_project.code, codes)
        self.assertNotIn(self.design_project.code, codes)

    def test_a_lead_cannot_open_a_brief_outside_their_lines(self):
        response = self.as_user(self.software_lead).get(
            f"/api/projects/{self.design_project.id}")
        self.assertEqual(response.status_code, 404)

    def test_a_lead_cannot_quote_a_brief_outside_their_lines(self):
        response = self.as_user(self.software_lead).post(
            f"/api/projects/{self.design_project.id}/quote", {"quote_usd": 500})
        self.assertEqual(response.status_code, 404)
        self.design_project.refresh_from_db()
        self.assertEqual(self.design_project.stage, Project.Stage.SUBMITTED)

    def test_a_lead_keeps_a_project_they_lead_after_losing_the_line(self):
        """Reassigning a lead's disciplines must not orphan work they own."""
        self.design_project.lead = self.software_lead
        self.design_project.save()
        codes = {p["code"] for p in self.as_user(self.software_lead).get("/api/projects").data}
        self.assertIn(self.design_project.code, codes)

    def test_a_superuser_sees_every_line(self):
        admin = User.objects.create_superuser("admin@ril.team", "x")
        codes = {p["code"] for p in self.as_user(admin).get("/api/projects").data}
        self.assertEqual(codes, {self.sw_project.code, self.design_project.code})

    def test_stats_are_scoped_to_the_same_projects_as_the_board(self):
        data = self.as_user(self.software_lead).get("/api/projects/stats/admin").data
        self.assertEqual(data["active_total"], 1)
        self.assertEqual([row["slug"] for row in data["by_line"]], ["software-web"])


class AssignmentTests(ScopingTestCase):
    def setUp(self):
        super().setUp()
        self.designer = User.objects.create_user(
            "designer@ril.dev", "x", full_name="A Designer", role=User.Role.EXPERT)
        self.designer.product_lines.add(self.design)
        self.designer.lead = self.design_lead
        self.designer.save()

        self.design_project.stage = Project.Stage.PAID
        self.design_project.quote_usd = 1000
        self.design_project.save()

    def test_an_expert_cannot_be_assigned_outside_their_discipline(self):
        self.sw_project.stage = Project.Stage.PAID
        self.sw_project.quote_usd = 1000
        self.sw_project.save()
        response = self.as_user(self.software_lead).post(
            f"/api/projects/{self.sw_project.id}/assign", {"expert": self.designer.id})
        self.assertEqual(response.status_code, 400)
        self.sw_project.refresh_from_db()
        self.assertIsNone(self.sw_project.expert)

    def test_an_expert_in_the_line_can_be_assigned(self):
        response = self.as_user(self.design_lead).post(
            f"/api/projects/{self.design_project.id}/assign",
            {"expert": self.designer.id, "tasks": ["Moodboard", "Concepts"]})
        self.assertEqual(response.status_code, 200)
        self.design_project.refresh_from_db()
        self.assertEqual(self.design_project.expert, self.designer)
        self.assertEqual(self.design_project.stage, Project.Stage.IN_PROGRESS)
        self.assertEqual(self.design_project.tasks.count(), 2)


class BriefRoutingTests(ScopingTestCase):
    def test_a_client_posts_into_a_product_line_and_service(self):
        service = Service.objects.get(product_line=self.design, name="UI/UX design")
        response = self.as_user(self.customer).post("/api/projects", {
            "title": "Redesign our app",
            "product_line": "design-creative",
            "service": service.id,
            "description": "Rework the onboarding flow.",
            "timeline": "2–4 weeks",
        })
        self.assertEqual(response.status_code, 201)
        project = Project.objects.get(id=response.data["id"])
        self.assertEqual(project.product_line, self.design)
        self.assertEqual(project.service, service)
        # The service name is snapshotted so the invoice survives a catalogue edit.
        self.assertEqual(project.category, "UI/UX design")

    def test_a_service_from_another_line_is_rejected(self):
        wrong = Service.objects.get(product_line=self.software, name="Mobile app")
        response = self.as_user(self.customer).post("/api/projects", {
            "title": "Redesign our app",
            "product_line": "design-creative",
            "service": wrong.id,
            "description": "Rework the onboarding flow.",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("service", response.data)

    def test_an_inactive_line_cannot_take_new_briefs(self):
        self.design.is_active = False
        self.design.save()
        response = self.as_user(self.customer).post("/api/projects", {
            "title": "Redesign our app",
            "product_line": "design-creative",
            "description": "Rework the onboarding flow.",
        })
        self.assertEqual(response.status_code, 400)

    def test_the_public_catalogue_hides_inactive_lines_and_services(self):
        self.design.is_active = False
        self.design.save()
        Service.objects.filter(product_line=self.software, name="Website").update(
            is_active=False)

        data = APIClient().get("/api/product-lines").data
        slugs = {line["slug"] for line in data}
        self.assertNotIn("design-creative", slugs)
        software = next(line for line in data if line["slug"] == "software-web")
        self.assertNotIn("Website", {s["name"] for s in software["services"]})
