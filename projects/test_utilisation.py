"""Billable coverage (G12).

The talent pitch is that 60% of a busy year beats 90% of a quiet one. That's
probably true and the platform could not previously demonstrate it — nothing
answered "what fraction of this expert's time actually had paid work on it?"

The arithmetic is simple; the honesty is where the tests are. A day with three
projects counts once, someone who joined last week isn't measured against last
quarter, and "nobody to measure" reports null rather than zero.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import ProductLine
from projects import utilisation as util
from projects.models import Project

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class CoverageTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "utlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "utexpert@ril.dev", "x", full_name="An Expert",
            role=User.Role.EXPERT, lead=self.lead)
        self.customer = User.objects.create_user(
            "utclient@acme.io", "x", role=User.Role.CLIENT)
        self.end = timezone.localdate()
        self.start = self.end - timedelta(days=29)
        # Everyone predates the window unless a test says otherwise.
        User.objects.filter(id__in=[self.expert.id]).update(
            date_joined=timezone.now() - timedelta(days=365))
        self.expert.refresh_from_db()

    def project(self, *, began_days_ago, closed_days_ago=None,
                stage=Project.Stage.IN_PROGRESS):
        p = Project.objects.create(
            title="A brief", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=stage, quote_usd=1000)
        p.experts.add(self.expert)
        Project.objects.filter(id=p.id).update(
            created_at=timezone.now() - timedelta(days=began_days_ago))
        if closed_days_ago is not None:
            Project.objects.filter(id=p.id).update(
                completed_at=timezone.now() - timedelta(days=closed_days_ago))
        p.refresh_from_db()
        return p

    def measure(self):
        return util.for_expert(self.expert, self.start, self.end)

    # --- the arithmetic ---
    def test_no_work_is_zero_coverage(self):
        row = self.measure()
        self.assertEqual(row["available_days"], 30)
        self.assertEqual(row["covered_days"], 0)
        self.assertEqual(row["coverage_percent"], 0.0)

    def test_a_project_running_all_window_is_full_coverage(self):
        self.project(began_days_ago=60)
        row = self.measure()
        self.assertEqual(row["covered_days"], 30)
        self.assertEqual(row["coverage_percent"], 100.0)

    def test_a_project_covering_half_the_window(self):
        self.project(began_days_ago=14)
        row = self.measure()
        self.assertEqual(row["covered_days"], 15)
        self.assertEqual(row["coverage_percent"], 50.0)

    def test_overlapping_projects_do_not_double_count(self):
        """The rule that stops a busy fortnight reading as 200%."""
        self.project(began_days_ago=60)
        self.project(began_days_ago=60)
        self.project(began_days_ago=60)
        row = self.measure()
        self.assertEqual(row["covered_days"], 30)
        self.assertLessEqual(row["coverage_percent"], 100.0)

    def test_two_separate_stints_add_up(self):
        self.project(began_days_ago=29, closed_days_ago=25,
                     stage=Project.Stage.COMPLETED)
        self.project(began_days_ago=9, closed_days_ago=5,
                     stage=Project.Stage.COMPLETED)
        row = self.measure()
        self.assertEqual(row["covered_days"], 10)

    def test_work_that_finished_before_the_window_does_not_count(self):
        self.project(began_days_ago=200, closed_days_ago=100,
                     stage=Project.Stage.COMPLETED)
        self.assertEqual(self.measure()["covered_days"], 0)

    def test_a_cancelled_project_stops_counting_when_it_stopped(self):
        p = self.project(began_days_ago=20, stage=Project.Stage.CANCELLED)
        Project.objects.filter(id=p.id).update(
            cancelled_at=timezone.now() - timedelta(days=10))
        row = self.measure()
        self.assertEqual(row["covered_days"], 11)

    def test_a_paid_but_unstarted_project_is_not_work(self):
        """The money arrived; nobody has been asked to start."""
        self.project(began_days_ago=20, stage=Project.Stage.PAID)
        self.assertEqual(self.measure()["covered_days"], 0)

    # --- honesty ---
    def test_a_mid_window_joiner_is_pro_rated(self):
        User.objects.filter(id=self.expert.id).update(
            date_joined=timezone.now() - timedelta(days=9))
        self.expert.refresh_from_db()
        self.project(began_days_ago=9)
        row = self.measure()
        self.assertEqual(row["available_days"], 10,
                         "measured against the time they were actually here")
        self.assertEqual(row["coverage_percent"], 100.0)

    def test_somebody_who_joined_after_the_window_reads_null(self):
        User.objects.filter(id=self.expert.id).update(
            date_joined=timezone.now() + timedelta(days=5))
        self.expert.refresh_from_db()
        row = self.measure()
        self.assertIsNone(row["coverage_percent"])
        self.assertEqual(row["available_days"], 0)

    def test_the_platform_figure_is_null_with_nobody_to_measure(self):
        """"Our experts are idle" and "we have no experts" are different."""
        User.objects.filter(role=User.Role.EXPERT).delete()
        result = util.platform(self.start, self.end)
        self.assertIsNone(result["avg_coverage_percent"])
        self.assertEqual(result["sample"], 0)

    def test_the_sample_size_travels_with_the_average(self):
        result = util.platform(self.start, self.end)
        self.assertEqual(result["sample"], 1)
        self.assertEqual(result["expert_count"], 1)


class UtilisationAccessTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "ualead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.other_lead = User.objects.create_user(
            "uaother@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.mine = User.objects.create_user(
            "uamine@ril.dev", "x", full_name="Mine",
            role=User.Role.EXPERT, lead=self.lead)
        self.theirs = User.objects.create_user(
            "uatheirs@ril.dev", "x", full_name="Theirs",
            role=User.Role.EXPERT, lead=self.other_lead)
        self.customer = User.objects.create_user(
            "uaclient@acme.io", "x", role=User.Role.CLIENT)
        self.admin = User.objects.create_superuser("uaboss@ril.team", "x")

    def test_an_expert_sees_only_their_own(self):
        response = as_user(self.mine).get("/api/utilisation")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["me"]["user_id"], self.mine.id)
        self.assertNotIn("experts", response.data)

    def test_a_lead_sees_their_roster_and_nobody_elses(self):
        response = as_user(self.lead).get("/api/utilisation")
        self.assertEqual(response.status_code, 200)
        names = {row["name"] for row in response.data["experts"]}
        self.assertEqual(names, {"Mine"})

    def test_a_lead_does_not_get_the_platform_figure(self):
        response = as_user(self.lead).get("/api/utilisation")
        self.assertNotIn("platform", response.data)

    def test_an_admin_gets_the_platform_figure(self):
        response = as_user(self.admin).get("/api/utilisation")
        self.assertEqual(response.status_code, 200)
        self.assertIn("platform", response.data)
        self.assertEqual(response.data["platform"]["expert_count"], 2)

    def test_a_client_gets_nothing(self):
        self.assertEqual(
            as_user(self.customer).get("/api/utilisation").status_code, 403)

    def test_the_roster_leads_with_the_idlest(self):
        """The useful person on this screen is the one about to quit."""
        busy = User.objects.create_user(
            "uabusy@ril.dev", "x", full_name="Busy",
            role=User.Role.EXPERT, lead=self.lead)
        project = Project.objects.create(
            title="Live work", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=busy, stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        project.experts.add(busy)
        Project.objects.filter(id=project.id).update(
            created_at=timezone.now() - timedelta(days=200))

        rows = as_user(self.lead).get("/api/utilisation").data["experts"]
        self.assertEqual(rows[0]["name"], "Mine", "idlest should lead")
        self.assertEqual(rows[-1]["name"], "Busy")
