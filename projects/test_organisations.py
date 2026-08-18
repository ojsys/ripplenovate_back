"""Client organisations (G9).

`User.company` was a free-text string, so one client meant one login. A real
buyer has a procurement contact, a project owner and a budget holder, and all
three need to see the work — which previously meant sharing a password or
posting briefs from three unconnected accounts.

This is the one change in the plan that **widens** a permission boundary, so
most of what follows is about the edges of that: a colleague sees the company's
work, a stranger still sees nothing, and a billing-only seat sees what things
cost without seeing the work itself.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Organisation, OrganisationMember
from catalog.models import ProductLine
from projects.models import Attachment, Project, Task

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class OrganisationAccessTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "orglead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "orgexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)

        self.acme = Organisation.objects.create(name="Acme Ltd", slug="acme-ltd")
        self.rival = Organisation.objects.create(name="Rival Inc", slug="rival-inc")

        self.buyer = self.member(self.acme, "buyer@acme.io", "A Buyer", "owner")
        self.colleague = self.member(self.acme, "pm@acme.io", "A Colleague", "member")
        self.finance = self.member(self.acme, "ap@acme.io", "A Bookkeeper", "billing")
        self.stranger = self.member(self.rival, "them@rival.io", "A Rival", "owner")

        self.project = Project.objects.create(
            title="A rebrand", client=self.buyer, organisation=self.acme,
            category="Brand identity", description="Our secret repositioning.",
            product_line=self.line, lead=self.lead, expert=self.expert,
            stage=Project.Stage.IN_PROGRESS, quote_usd=10000)
        self.project.experts.add(self.expert)
        Attachment.objects.create(
            project=self.project, url="https://figma.com/file/x", label="Brand deck",
            purpose=Attachment.Purpose.DELIVERABLE, added_by=self.expert)
        Task.objects.create(
            project=self.project, title="Logo", assignee=self.expert,
            amount_usd=Decimal("2000"))

    def member(self, org, email, name, role):
        user = User.objects.create_user(
            email, "x", full_name=name, role=User.Role.CLIENT)
        OrganisationMember.objects.create(organisation=org, user=user, role=role)
        return user

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    # --- the widening ---
    def test_a_colleague_sees_the_companys_project(self):
        response = as_user(self.colleague).get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "A rebrand")

    def test_a_colleague_sees_it_in_their_list(self):
        rows = as_user(self.colleague).get("/api/projects").data
        self.assertEqual([r["id"] for r in rows], [self.project.id])

    def test_the_person_who_posted_it_still_sees_it(self):
        self.assertEqual(as_user(self.buyer).get(self.url()).status_code, 200)

    # --- the boundary that must not move ---
    def test_another_companys_client_sees_nothing(self):
        self.assertIn(as_user(self.stranger).get(self.url()).status_code, (403, 404))
        self.assertEqual(as_user(self.stranger).get("/api/projects").data, [])

    def test_a_client_with_no_organisation_sees_only_their_own(self):
        loner = User.objects.create_user(
            "loner@nowhere.io", "x", role=User.Role.CLIENT)
        own = Project.objects.create(
            title="Mine", client=loner, category="Brand identity",
            description="…", product_line=self.line,
            stage=Project.Stage.SUBMITTED, quote_usd=100)
        rows = as_user(loner).get("/api/projects").data
        self.assertEqual([r["id"] for r in rows], [own.id])

    def test_a_project_with_no_organisation_stays_with_its_author(self):
        """Pre-migration rows, and anything an admin created directly."""
        orphan = Project.objects.create(
            title="Legacy", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line,
            stage=Project.Stage.SUBMITTED, quote_usd=100)
        self.assertEqual(
            as_user(self.buyer).get(f"/api/projects/{orphan.id}").status_code, 200)
        self.assertIn(
            as_user(self.colleague).get(f"/api/projects/{orphan.id}").status_code,
            (403, 404))

    # --- the billing seat ---
    def test_a_billing_seat_reaches_the_project(self):
        """They have an invoice to settle."""
        self.assertEqual(as_user(self.finance).get(self.url()).status_code, 200)

    def test_a_billing_seat_sees_the_money_and_not_the_work(self):
        data = as_user(self.finance).get(self.url()).data
        self.assertEqual(data["quote_usd"], 10000)
        self.assertEqual(data["attachments"], [], "deliverables leaked to finance")
        self.assertEqual(data["activity"], [], "the feed leaked to finance")
        self.assertEqual(data["tasks"], [], "the task list leaked to finance")

    def test_everyone_else_still_sees_everything(self):
        for who in (self.buyer, self.colleague, self.lead, self.expert):
            with self.subTest(who=who.email):
                data = as_user(who).get(self.url()).data
                self.assertEqual(len(data["attachments"]), 1)
                self.assertEqual(len(data["tasks"]), 1)

    def test_a_billing_seat_cannot_post_a_brief(self):
        response = as_user(self.finance).post(
            "/api/projects",
            {"title": "Sneaky", "category": "Brand identity",
             "description": "…", "product_line": self.line.slug},
            format="json")
        self.assertEqual(response.status_code, 403)
        self.assertIn("billing-only", str(response.data))

    # --- posting attaches to the company ---
    def test_a_new_brief_belongs_to_the_company(self):
        response = as_user(self.colleague).post(
            "/api/projects",
            {"title": "Another", "category": "Brand identity",
             "description": "…", "product_line": self.line.slug},
            format="json")
        self.assertEqual(response.status_code, 201, response.data)
        fresh = Project.objects.get(title="Another")
        self.assertEqual(fresh.organisation_id, self.acme.id)
        self.assertEqual(fresh.client_id, self.colleague.id,
                         "the individual who posted is still recorded")
        self.assertEqual(fresh.company, "Acme Ltd")

    def test_a_colleagues_brief_is_visible_to_the_owner(self):
        as_user(self.colleague).post(
            "/api/projects",
            {"title": "Another", "category": "Brand identity",
             "description": "…", "product_line": self.line.slug},
            format="json")
        titles = {r["title"] for r in as_user(self.buyer).get("/api/projects").data}
        self.assertIn("Another", titles)


class OrganisationManagementTests(TestCase):
    def setUp(self):
        self.acme = Organisation.objects.create(name="Acme Ltd", slug="acme-ltd")
        self.owner = User.objects.create_user(
            "mgowner@acme.io", "x", full_name="An Owner", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=self.acme, user=self.owner, role="owner")
        self.member = User.objects.create_user(
            "mgmember@acme.io", "x", full_name="A Member", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=self.acme, user=self.member, role="member")
        self.outsider = User.objects.create_user(
            "mgout@elsewhere.io", "x", full_name="Outsider", role=User.Role.CLIENT)
        self.expert = User.objects.create_user(
            "mgexpert@ril.dev", "x", role=User.Role.EXPERT)

    def test_a_member_reads_the_company(self):
        data = as_user(self.member).get("/api/organisation").data
        self.assertEqual(data["name"], "Acme Ltd")
        self.assertEqual(data["my_role"], "member")
        self.assertFalse(data["can_manage"])
        self.assertEqual(len(data["members"]), 2)

    def test_only_an_owner_renames_it(self):
        self.assertEqual(
            as_user(self.member).patch(
                "/api/organisation", {"name": "Hijacked"}, format="json").status_code,
            403)
        response = as_user(self.owner).patch(
            "/api/organisation", {"name": "Acme Group"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.acme.refresh_from_db()
        self.assertEqual(self.acme.name, "Acme Group")

    def test_an_owner_adds_an_existing_account(self):
        response = as_user(self.owner).post(
            "/api/organisation/members",
            {"email": self.outsider.email, "role": "billing"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(OrganisationMember.objects.filter(
            organisation=self.acme, user=self.outsider, role="billing").exists())

    def test_an_unknown_email_is_refused_rather_than_invented(self):
        """Turning a guessed address into a login is not something this does."""
        response = as_user(self.owner).post(
            "/api/organisation/members",
            {"email": "nobody@example.com"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Ask them to sign up", str(response.data))

    def test_the_delivery_side_cannot_be_added_as_a_buyer(self):
        response = as_user(self.owner).post(
            "/api/organisation/members",
            {"email": self.expert.email}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_member_cannot_add_people(self):
        self.assertEqual(
            as_user(self.member).post(
                "/api/organisation/members",
                {"email": self.outsider.email}, format="json").status_code, 403)

    def test_adding_somebody_twice_is_refused(self):
        as_user(self.owner).post(
            "/api/organisation/members",
            {"email": self.outsider.email}, format="json")
        response = as_user(self.owner).post(
            "/api/organisation/members",
            {"email": self.outsider.email}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_seat_can_be_changed(self):
        response = as_user(self.owner).patch(
            f"/api/organisation/members/{self.member.id}",
            {"role": "billing"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OrganisationMember.objects.get(user=self.member).role, "billing")

    def test_somebody_can_be_removed(self):
        response = as_user(self.owner).delete(
            f"/api/organisation/members/{self.member.id}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            OrganisationMember.objects.filter(user=self.member).exists())

    def test_the_last_owner_cannot_step_down(self):
        """Otherwise the company is left with projects nobody can administer."""
        for payload in ({"role": "member"}, {"role": "billing"}):
            with self.subTest(payload=payload):
                response = as_user(self.owner).patch(
                    f"/api/organisation/members/{self.owner.id}",
                    payload, format="json")
                self.assertEqual(response.status_code, 400)

    def test_the_last_owner_cannot_be_removed(self):
        response = as_user(self.owner).delete(
            f"/api/organisation/members/{self.owner.id}")
        self.assertEqual(response.status_code, 400)

    def test_a_second_owner_frees_the_first(self):
        as_user(self.owner).patch(
            f"/api/organisation/members/{self.member.id}",
            {"role": "owner"}, format="json")
        response = as_user(self.owner).delete(
            f"/api/organisation/members/{self.owner.id}")
        self.assertEqual(response.status_code, 200)

    def test_being_added_is_announced(self):
        mail.outbox = []
        as_user(self.owner).post(
            "/api/organisation/members",
            {"email": self.outsider.email}, format="json")
        to = {addr for m in mail.outbox for addr in m.to}
        self.assertEqual(to, {self.outsider.email})

    def test_a_billing_seat_is_told_what_they_will_and_wont_see(self):
        mail.outbox = []
        as_user(self.owner).post(
            "/api/organisation/members",
            {"email": self.outsider.email, "role": "billing"}, format="json")
        body = " ".join(m.body for m in mail.outbox)
        self.assertIn("invoices", body.lower())


class OrganisationSignupTests(TestCase):
    def test_a_new_client_gets_an_organisation(self):
        response = APIClient().post("/api/auth/register", {
            "email": "new@acme.io", "full_name": "New Buyer",
            "company": "Acme Ltd", "password": "Str0ng-Pass!23",
            "role": "client",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(email="new@acme.io")
        self.assertTrue(user.organisation_memberships.exists())
        self.assertEqual(
            user.organisation_memberships.get().organisation.name, "Acme Ltd")

    def test_the_first_person_at_a_company_owns_it(self):
        APIClient().post("/api/auth/register", {
            "email": "first@acme.io", "full_name": "First",
            "company": "Acme Ltd", "password": "Str0ng-Pass!23", "role": "client",
        }, format="json")
        self.assertEqual(
            User.objects.get(email="first@acme.io")
            .organisation_memberships.get().role, "owner")

    def test_the_second_joins_as_a_member(self):
        """A matching company name is not proof of authority over it."""
        for email in ("first@acme.io", "second@acme.io"):
            APIClient().post("/api/auth/register", {
                "email": email, "full_name": "Somebody", "company": "Acme Ltd",
                "password": "Str0ng-Pass!23", "role": "client",
            }, format="json")
        second = User.objects.get(email="second@acme.io")
        self.assertEqual(second.organisation_memberships.get().role, "member")
        self.assertEqual(Organisation.objects.filter(name="Acme Ltd").count(), 1)

    def test_case_and_spacing_variants_land_in_one_company(self):
        for email, company in (("a@acme.io", "Acme Ltd"),
                               ("b@acme.io", "acme  ltd"),
                               ("c@acme.io", "  ACME LTD ")):
            APIClient().post("/api/auth/register", {
                "email": email, "full_name": "Somebody", "company": company,
                "password": "Str0ng-Pass!23", "role": "client",
            }, format="json")
        self.assertEqual(Organisation.objects.count(), 1)

    def test_two_genuinely_different_companies_stay_apart(self):
        """Nothing cleverer than case folding — merging real companies wrongly
        would show one buyer another's briefs."""
        for email, company in (("a@acme.io", "Acme Ltd"),
                               ("b@acme.io", "Acme Limited")):
            APIClient().post("/api/auth/register", {
                "email": email, "full_name": "Somebody", "company": company,
                "password": "Str0ng-Pass!23", "role": "client",
            }, format="json")
        self.assertEqual(Organisation.objects.count(), 2)

    def test_a_sole_trader_gets_a_personal_organisation(self):
        APIClient().post("/api/auth/register", {
            "email": "solo@nowhere.io", "full_name": "Solo Trader",
            "password": "Str0ng-Pass!23", "role": "client",
        }, format="json")
        org = User.objects.get(
            email="solo@nowhere.io").organisation_memberships.get().organisation
        self.assertEqual(org.name, "Solo Trader")

    def test_a_delivery_lead_gets_none(self):
        APIClient().post("/api/auth/register", {
            "email": "lead@ril.team", "full_name": "A Lead",
            "password": "Str0ng-Pass!23", "role": "delivery_lead",
        }, format="json")
        self.assertEqual(Organisation.objects.count(), 0)


class BackfillMigrationTests(TestCase):
    """The migration that turns `User.company` strings into real companies.

    Untested by simply running the suite: the migration executes against an
    empty database, long before any of these fixtures exist. So it is called
    directly here, against data shaped like production's.
    """

    def _migration(self):
        # Imported by path — the module name starts with a digit.
        import importlib

        return importlib.import_module(
            "projects.migrations.0028_backfill_organisations")

    def _apps(self):
        from django.apps import apps

        return apps

    def client_user(self, email, company="", name="Somebody"):
        return User.objects.create_user(
            email, "x", full_name=name, company=company, role=User.Role.CLIENT)

    def brief(self, client):
        return Project.objects.create(
            title="A brief", client=client, category="Brand identity",
            description="…", stage=Project.Stage.SUBMITTED, quote_usd=1000)

    def migrate(self):
        self._migration().backfill(self._apps(), None)

    def test_one_company_per_distinct_name(self):
        a = self.client_user("a@acme.io", "Acme Ltd")
        b = self.client_user("b@acme.io", "Acme Ltd")
        self.client_user("c@rival.io", "Rival Inc")
        self.migrate()

        self.assertEqual(Organisation.objects.count(), 2)
        self.assertEqual(
            a.organisation_memberships.get().organisation_id,
            b.organisation_memberships.get().organisation_id,
        )

    def test_case_and_spacing_variants_fold_together(self):
        self.client_user("a@acme.io", "Acme Ltd")
        self.client_user("b@acme.io", "  acme   ltd ")
        self.client_user("c@acme.io", "ACME LTD")
        self.migrate()
        self.assertEqual(Organisation.objects.count(), 1)

    def test_near_misses_are_left_apart(self):
        """Wrongly merging two real companies would show one buyer another's
        briefs — a far worse outcome than two rows an admin can tidy."""
        self.client_user("a@acme.io", "Acme Ltd")
        self.client_user("b@acme.io", "Acme Limited")
        self.migrate()
        self.assertEqual(Organisation.objects.count(), 2)

    def test_a_blank_company_gets_a_personal_one(self):
        solo = self.client_user("solo@nowhere.io", "", name="Solo Trader")
        self.migrate()
        org = solo.organisation_memberships.get().organisation
        self.assertEqual(org.name, "Solo Trader")

    def test_everyone_starts_as_an_owner_of_their_own(self):
        a = self.client_user("a@acme.io", "Acme Ltd")
        self.migrate()
        self.assertEqual(a.organisation_memberships.get().role, "owner")

    def test_projects_are_attached(self):
        a = self.client_user("a@acme.io", "Acme Ltd")
        first, second = self.brief(a), self.brief(a)
        self.migrate()
        org = a.organisation_memberships.get().organisation
        for project in (first, second):
            project.refresh_from_db()
            self.assertEqual(project.organisation_id, org.id)

    def test_colleagues_projects_land_in_the_same_company(self):
        a = self.client_user("a@acme.io", "Acme Ltd")
        b = self.client_user("b@acme.io", "acme ltd")
        pa, pb = self.brief(a), self.brief(b)
        self.migrate()
        pa.refresh_from_db(); pb.refresh_from_db()
        self.assertEqual(pa.organisation_id, pb.organisation_id)

    def test_slugs_never_collide(self):
        """Two different companies can slugify to the same thing."""
        self.client_user("a@x.io", "Acme & Co")
        self.client_user("b@y.io", "Acme + Co")
        self.migrate()
        slugs = list(Organisation.objects.values_list("slug", flat=True))
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(Organisation.objects.count(), 2)

    def test_it_is_idempotent(self):
        a = self.client_user("a@acme.io", "Acme Ltd")
        self.brief(a)
        self.migrate()
        self.migrate()
        self.assertEqual(Organisation.objects.count(), 1)
        self.assertEqual(OrganisationMember.objects.count(), 1)

    def test_the_delivery_side_gets_nothing(self):
        User.objects.create_user(
            "lead@ril.team", "x", company="Ripple", role=User.Role.DELIVERY_LEAD)
        User.objects.create_user(
            "exp@ril.dev", "x", company="Ripple", role=User.Role.EXPERT)
        self.migrate()
        self.assertEqual(Organisation.objects.count(), 0)

    def test_the_company_field_is_left_untouched(self):
        """Nothing here rewrites `User.company` — the reverse depends on it."""
        a = self.client_user("a@acme.io", "Acme Ltd")
        self.migrate()
        a.refresh_from_db()
        self.assertEqual(a.company, "Acme Ltd")

    def test_it_reverses_cleanly(self):
        a = self.client_user("a@acme.io", "Acme Ltd")
        project = self.brief(a)
        self.migrate()
        self._migration().unbackfill(self._apps(), None)

        project.refresh_from_db()
        self.assertIsNone(project.organisation_id)
        self.assertEqual(Organisation.objects.count(), 0)
        self.assertEqual(OrganisationMember.objects.count(), 0)
