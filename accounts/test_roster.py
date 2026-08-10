"""Picking up an expert who already has an account.

An expert works across teams if they have the capacity — which lead first
signed them up isn't the limit, and it needed no admin to change. Two things
follow: a lead can add an existing expert to their roster, and the assignment
picker isn't fenced to their own roster either.
"""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from projects.models import Project

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class RosterTests(TestCase):
    def setUp(self):
        self.web = ProductLine.objects.get(slug="software-web")
        self.design = ProductLine.objects.get(slug="design-creative")

        self.lead = User.objects.create_user(
            "rlead@ril.team", "x", full_name="My Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.web)
        self.other_lead = User.objects.create_user(
            "rother@ril.team", "x", full_name="Other Lead",
            role=User.Role.DELIVERY_LEAD)
        self.other_lead.product_lines.add(self.design)

        # The case that started this: an expert account from before, on nobody's
        # roster, unreachable and un-invitable.
        self.stranded = User.objects.create_user(
            "stranded@ril.dev", "x", full_name="Stranded Expert",
            role=User.Role.EXPERT)
        self.stranded.product_lines.add(self.design)
        self.theirs = User.objects.create_user(
            "theirs@ril.dev", "x", full_name="Their Expert",
            role=User.Role.EXPERT, lead=self.other_lead)
        self.theirs.product_lines.add(self.design)
        self.customer = User.objects.create_user(
            "rclient@acme.io", "x", role=User.Role.CLIENT)

    def add(self, expert, by=None):
        return as_user(by or self.lead).post(f"/api/users/{expert.id}/roster")

    # --- claiming ---
    def test_a_lead_adds_a_stranded_expert_without_an_admin(self):
        response = self.add(self.stranded)
        self.assertEqual(response.status_code, 200)
        self.stranded.refresh_from_db()
        self.assertEqual(self.stranded.lead_id, self.lead.id)
        self.assertTrue(response.data["on_my_roster"])

    def test_they_gain_the_lines_their_new_lead_runs(self):
        """Otherwise the move is a dead end: an expert can only be assigned in
        a discipline they cover."""
        self.add(self.stranded)
        slugs = set(self.stranded.product_lines.values_list("slug", flat=True))
        self.assertIn(self.web.slug, slugs, "can't be assigned by their new lead")
        self.assertIn(self.design.slug, slugs, "lost what they already covered")

    def test_an_expert_on_another_roster_can_move(self):
        self.assertEqual(self.add(self.theirs).status_code, 200)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.lead_id, self.lead.id)

    def test_the_lead_who_loses_them_is_told(self):
        """No admin gate, but not silent either."""
        mail.outbox = []
        self.add(self.theirs)
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.other_lead.email, recipients)
        self.assertIn(self.theirs.email, recipients)

    def test_adding_someone_already_yours_says_so(self):
        self.add(self.stranded)
        response = self.add(self.stranded)
        self.assertEqual(response.status_code, 400)
        self.assertIn("already on your team", str(response.data))

    def test_only_experts_can_be_added(self):
        self.assertEqual(self.add(self.customer).status_code, 404)

    def test_a_client_cannot_build_a_roster(self):
        self.assertEqual(self.add(self.stranded, by=self.customer).status_code, 403)

    def test_a_lead_can_let_someone_go(self):
        self.add(self.stranded)
        response = as_user(self.lead).delete(f"/api/users/{self.stranded.id}/roster")
        self.assertEqual(response.status_code, 200)
        self.stranded.refresh_from_db()
        self.assertIsNone(self.stranded.lead_id)

    def test_you_cannot_release_someone_who_is_not_yours(self):
        self.assertEqual(
            as_user(self.lead).delete(f"/api/users/{self.theirs.id}/roster").status_code,
            400)

    def test_work_they_already_hold_is_untouched_by_a_move(self):
        """Their projects and earnings belong to them, not to a roster."""
        project = Project.objects.create(
            title="Ongoing", client=self.customer, category="Brand identity",
            description="…", product_line=self.design, lead=self.other_lead,
            expert=self.theirs, stage=Project.Stage.IN_PROGRESS, quote_usd=1000)
        project.experts.add(self.theirs)
        self.add(self.theirs)
        project.refresh_from_db()
        self.assertIn(self.theirs.id, {e.id for e in project.experts.all()})
        self.assertEqual(project.expert_id, self.theirs.id)


class ExpertPickerTests(TestCase):
    """The picker used to be fenced to `mine=1`, which is what made an expert
    on someone else's roster unreachable."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "plead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.mine = User.objects.create_user(
            "pmine@ril.dev", "x", full_name="Mine", role=User.Role.EXPERT,
            lead=self.lead)
        self.theirs = User.objects.create_user(
            "ptheirs@ril.dev", "x", full_name="Theirs", role=User.Role.EXPERT)
        for e in (self.mine, self.theirs):
            e.product_lines.add(self.line)
        self.elsewhere = User.objects.create_user(
            "pelse@ril.dev", "x", role=User.Role.EXPERT)
        self.elsewhere.product_lines.add(
            ProductLine.objects.get(slug="software-web"))

    def picker(self, **params):
        return as_user(self.lead).get("/api/users/experts", params).data

    def test_the_picker_reaches_beyond_your_own_roster(self):
        names = {d["full_name"] for d in self.picker(product_line=self.line.slug)}
        self.assertEqual(names, {"Mine", "Theirs"})

    def test_each_one_says_whether_they_are_yours(self):
        by_name = {d["full_name"]: d for d in self.picker(product_line=self.line.slug)}
        self.assertTrue(by_name["Mine"]["on_my_roster"])
        self.assertFalse(by_name["Theirs"]["on_my_roster"])

    def test_the_discipline_filter_still_applies(self):
        """Beyond your roster, not beyond the brief's discipline."""
        names = {d["full_name"] for d in self.picker(product_line=self.line.slug)}
        self.assertNotIn(self.elsewhere.full_name, names)

    def test_mine_still_narrows_when_asked(self):
        names = {d["full_name"] for d in self.picker(mine=1)}
        self.assertEqual(names, {"Mine"})


class ActiveLoadTests(TestCase):
    """"Can they handle it?" needs a number that moves."""

    def setUp(self):
        self.expert = User.objects.create_user(
            "load@ril.dev", "x", role=User.Role.EXPERT, active_load=99)
        self.lead = User.objects.create_user(
            "loadlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.customer = User.objects.create_user(
            "loadclient@acme.io", "x", role=User.Role.CLIENT)

    def project(self, stage):
        p = Project.objects.create(
            title="A brief", client=self.customer, category="Web application",
            description="…", stage=stage, quote_usd=1000)
        p.experts.add(self.expert)
        return p

    def count(self):
        return as_user(self.lead).get("/api/users/experts").data[0]["active_projects"]

    def test_it_counts_live_projects_not_the_stored_number(self):
        self.assertEqual(self.count(), 0, "the stored active_load=99 is fiction")
        self.project(Project.Stage.IN_PROGRESS)
        self.project(Project.Stage.REVIEW)
        self.assertEqual(self.count(), 2)

    def test_finished_and_unstarted_work_does_not_count(self):
        self.project(Project.Stage.COMPLETED)
        self.project(Project.Stage.QUOTED)
        self.assertEqual(self.count(), 0)
