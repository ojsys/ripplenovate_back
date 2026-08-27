"""Building a delivery team on a project (step B).

Access follows team membership rather than a single `expert` field, and a lead
can add and remove people after delivery has started. Payouts are untouched —
the whole expert share still goes to the primary expert on completion — so the
money-shaped assertions here are about what removal *refuses* to do.
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


class TeamTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.other_line = ProductLine.objects.exclude(slug="design-creative").first()

        self.lead = User.objects.create_user(
            "teamlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)

        self.ada = User.objects.create_user(
            "ada@ril.dev", "x", full_name="Ada Eze", role=User.Role.EXPERT)
        self.chidi = User.objects.create_user(
            "chidi@ril.dev", "x", full_name="Chidi Okonkwo", role=User.Role.EXPERT)
        for expert in (self.ada, self.chidi):
            expert.product_lines.add(self.line)
        # Works a different discipline — can't be put on this brief.
        self.outsider = User.objects.create_user(
            "wrongline@ril.dev", "x", full_name="Wrong Line", role=User.Role.EXPERT)
        self.outsider.product_lines.add(self.other_line)

        self.customer = User.objects.create_user(
            "teamclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            stage=Project.Stage.PAID, quote_usd=5000)

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    def assign(self, **payload):
        return as_user(self.lead).post(self.url("/assign"), payload, format="json")

    # --- assignment ---
    def test_a_lead_assigns_a_whole_team_at_once(self):
        response = self.assign(experts=[self.ada.id, self.chidi.id],
                               tasks=["Wireframes", "Build"])
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(
            {e.id for e in self.project.experts.all()}, {self.ada.id, self.chidi.id})
        self.assertEqual(self.project.stage, Project.Stage.IN_PROGRESS)

    def test_the_first_expert_named_becomes_the_primary(self):
        self.assign(experts=[self.chidi.id, self.ada.id])
        self.project.refresh_from_db()
        self.assertEqual(self.project.expert_id, self.chidi.id)

    def test_the_old_single_expert_payload_still_works(self):
        """What the assign screen sends today, until the frontend catches up."""
        response = self.assign(expert=self.ada.id, tasks=["Wireframes"])
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.expert_id, self.ada.id)
        self.assertEqual([e.id for e in self.project.experts.all()], [self.ada.id])

    def test_every_member_is_checked_against_the_discipline(self):
        """A team is only as valid as its least suitable member."""
        response = self.assign(experts=[self.ada.id, self.outsider.id])
        self.assertEqual(response.status_code, 400)
        self.project.refresh_from_db()
        self.assertEqual(self.project.experts.count(), 0)
        self.assertEqual(self.project.stage, Project.Stage.PAID, "delivery started anyway")

    def test_your_own_roster_is_not_held_to_the_discipline_tag(self):
        """Vouching for them outranks a tag nobody curates.

        The tag is copied from whichever lead signed the expert up, and no lead
        can edit it — so a lead whose own lines don't match the brief could not
        staff their own team on it by any route in the product.
        """
        self.outsider.lead = self.lead
        self.outsider.save(update_fields=["lead"])
        response = self.assign(experts=[self.outsider.id])
        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.expert_id, self.outsider.id)

    def test_another_leads_expert_is_still_held_to_it(self):
        """Past your own roster the tag is the only thing there is to go on."""
        other_lead = User.objects.create_user(
            "otherteamlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.outsider.lead = other_lead
        self.outsider.save(update_fields=["lead"])
        self.assertEqual(self.assign(experts=[self.outsider.id]).status_code, 400)

    def test_assigning_nobody_is_refused(self):
        self.assertEqual(self.assign(experts=[]).status_code, 400)

    # --- adding after kickoff ---
    def test_a_lead_adds_an_expert_mid_delivery(self):
        self.assign(expert=self.ada.id)
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.chidi.id]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({m["id"] for m in response.data["team"]},
                         {self.ada.id, self.chidi.id})
        # Adding someone doesn't unseat the person already answerable for it.
        self.project.refresh_from_db()
        self.assertEqual(self.project.expert_id, self.ada.id)

    def test_adding_someone_already_on_the_team_changes_nothing(self):
        self.assign(expert=self.ada.id)
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.ada.id]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.project.experts.count(), 1)

    def test_building_the_first_team_here_kicks_off_delivery(self):
        """The project page can start delivery too, not just the assign screen.

        It used to leave the brief sitting at Paid, which the rest of the
        platform reads as "nobody has been asked to start" — so the expert's
        board skipped it and the task they'd just been given was invisible to
        them.
        """
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.ada.id]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.IN_PROGRESS)
        self.assertEqual(self.project.expert_id, self.ada.id)

    def test_a_task_assigned_this_way_reaches_the_experts_board(self):
        """The whole complaint, end to end."""
        as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.chidi.id]}, format="json")
        created = as_user(self.lead).post(
            self.url("/tasks"),
            {"title": "Their piece", "assignee": self.chidi.id,
             "amount_usd": "250"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        board = as_user(self.chidi).get("/api/projects")
        listed = {j["id"]: j["stage"] for j in board.data}
        self.assertEqual(listed.get(self.project.id),
                         Project.Stage.IN_PROGRESS.value)
        detail = as_user(self.chidi).get(self.url())
        self.assertIn(
            self.chidi.id,
            [t["assignee"] for t in detail.data["tasks"]])

    def test_adding_someone_mid_delivery_does_not_move_the_stage(self):
        self.assign(expert=self.ada.id)
        self.project.stage = Project.Stage.REVIEW
        self.project.save(update_fields=["stage"])
        as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.chidi.id]}, format="json")
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.REVIEW)

    def test_the_team_cannot_be_built_before_the_client_pays(self):
        self.project.stage = Project.Stage.QUOTED
        self.project.save(update_fields=["stage"])
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.ada.id]}, format="json")
        self.assertEqual(response.status_code, 400)

    # --- removal ---
    def test_a_lead_removes_an_expert_who_holds_nothing(self):
        self.assign(experts=[self.ada.id, self.chidi.id])
        response = as_user(self.lead).delete(self.url(f"/experts/{self.chidi.id}"))
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual([e.id for e in self.project.experts.all()], [self.ada.id])

    def test_removing_an_expert_unassigns_their_unpriced_tasks(self):
        self.assign(experts=[self.ada.id, self.chidi.id])
        task = Task.objects.create(project=self.project, title="Loose end",
                                   assignee=self.chidi)
        as_user(self.lead).delete(self.url(f"/experts/{self.chidi.id}"))
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)
        # Said out loud in the feed rather than happening quietly.
        self.assertTrue(self.project.activity.filter(
            text__contains="now unassigned").exists())

    def test_an_expert_holding_a_priced_task_cannot_be_removed(self):
        self.assign(experts=[self.ada.id, self.chidi.id])
        Task.objects.create(project=self.project, title="Paid work",
                            assignee=self.chidi, amount_usd=Decimal("500.00"))
        response = as_user(self.lead).delete(self.url(f"/experts/{self.chidi.id}"))
        self.assertEqual(response.status_code, 400)
        self.project.refresh_from_db()
        self.assertIn(self.chidi.id, {e.id for e in self.project.experts.all()})

    def test_an_expert_who_has_been_paid_cannot_be_removed(self):
        self.assign(experts=[self.ada.id, self.chidi.id])
        Earning.objects.create(
            project=self.project, user=self.chidi, kind=Earning.Kind.EXPERT,
            share_percent=Decimal("10.00"), amount_usd=Decimal("500.00"))
        response = as_user(self.lead).delete(self.url(f"/experts/{self.chidi.id}"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("already been paid", str(response.data))

    def test_removing_the_primary_hands_the_role_to_whoever_is_left(self):
        self.assign(experts=[self.ada.id, self.chidi.id])
        as_user(self.lead).delete(self.url(f"/experts/{self.ada.id}"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.expert_id, self.chidi.id)

    def test_removing_the_last_expert_leaves_no_primary(self):
        self.assign(expert=self.ada.id)
        as_user(self.lead).delete(self.url(f"/experts/{self.ada.id}"))
        self.project.refresh_from_db()
        self.assertIsNone(self.project.expert_id)
        self.assertEqual(self.project.experts.count(), 0)

    def test_removing_someone_who_was_never_on_the_team_is_refused(self):
        self.assign(expert=self.ada.id)
        response = as_user(self.lead).delete(self.url(f"/experts/{self.chidi.id}"))
        self.assertEqual(response.status_code, 400)


class TeamAccessTests(TestCase):
    """Access follows the team, not the single `expert` column."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "acclead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.primary = User.objects.create_user(
            "primary@ril.dev", "x", full_name="Primary", role=User.Role.EXPERT)
        self.second = User.objects.create_user(
            "second@ril.dev", "x", full_name="Second", role=User.Role.EXPERT)
        self.stranger = User.objects.create_user(
            "stranger@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "accclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.primary, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.primary, self.second)
        self.task = Task.objects.create(
            project=self.project, title="A task", assignee=self.second)

    def test_a_second_expert_sees_the_project_on_their_board(self):
        codes = {p["code"] for p in as_user(self.second).get("/api/projects").data}
        self.assertIn(self.project.code, codes)

    def test_a_second_expert_can_open_it_and_work_it(self):
        client = as_user(self.second)
        base = f"/api/projects/{self.project.id}"
        self.assertEqual(client.get(base).status_code, 200)
        self.assertEqual(
            client.post(f"{base}/activity", {"text": "Progress", "kind": "progress"},
                        format="json").status_code, 200)
        self.assertEqual(
            client.post(f"{base}/attachments", {"url": "https://figma.com/file/x"},
                        format="json").status_code, 201)
        self.assertEqual(client.post(f"{base}/submit-review").status_code, 200)

    def test_a_second_expert_does_not_maintain_the_task_list(self):
        """Delivering the work and pricing it are different jobs."""
        response = as_user(self.second).patch(
            f"/api/tasks/{self.task.id}", {"amount_usd": "900.00"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_an_expert_not_on_the_team_still_sees_nothing(self):
        client = as_user(self.stranger)
        self.assertEqual(len(client.get("/api/projects").data), 0)
        self.assertEqual(
            client.get(f"/api/projects/{self.project.id}").status_code, 404)
        self.assertEqual(
            client.patch(f"/api/tasks/{self.task.id}", {"title": "Mine now"},
                         format="json").status_code, 403)

    def test_a_primary_expert_missing_from_the_team_still_gets_in(self):
        """An admin can set `expert` directly without touching the team list.
        Locking the primary expert out of their own project would be an odd way
        to discover that."""
        self.project.experts.remove(self.primary)
        client = as_user(self.primary)
        self.assertEqual(
            client.get(f"/api/projects/{self.project.id}").status_code, 200)
        codes = {p["code"] for p in client.get("/api/projects").data}
        self.assertIn(self.project.code, codes)

    def test_the_detail_payload_names_the_team(self):
        data = as_user(self.lead).get(f"/api/projects/{self.project.id}").data
        self.assertEqual([m["full_name"] for m in data["team"]],
                         ["Primary", "Second"])
        self.assertEqual([m["is_primary"] for m in data["team"]], [True, False])

    def test_everyone_on_the_team_is_notified_of_an_update(self):
        from django.core import mail

        mail.outbox = []
        as_user(self.lead).post(
            f"/api/projects/{self.project.id}/activity",
            {"text": "Checking in", "kind": "update"}, format="json")
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.primary.email, recipients)
        self.assertIn(self.second.email, recipients)


class AddingToALiveProjectTests(TestCase):
    """Putting somebody on a project that's already running.

    The reported bug: a lead's roster showed experts in **My Team** who could
    not be given a task. The task assignee list is the *project's* team, so
    anyone not picked at kickoff was unreachable — and the one control that
    looked like it would help pointed at the assign screen, which does nothing
    once the project has left Paid.

    The endpoint was always there. Nothing called it.
    """

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "livelead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.picked = User.objects.create_user(
            "livepicked@ril.dev", "x", full_name="Picked At Kickoff",
            role=User.Role.EXPERT, lead=self.lead)
        self.picked.product_lines.add(self.line)
        # On the lead's roster, but not on the project. This is the person the
        # bug made unreachable.
        self.bench = User.objects.create_user(
            "livebench@ril.dev", "x", full_name="On My Team",
            role=User.Role.EXPERT, lead=self.lead)
        self.bench.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "liveclient@acme.io", "x", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.picked, stage=Project.Stage.IN_PROGRESS, quote_usd=5000)
        self.project.experts.add(self.picked)

    def url(self, suffix=""):
        return f"/api/projects/{self.project.id}{suffix}"

    def test_a_roster_expert_starts_off_the_project(self):
        """The state the bug report describes."""
        team = as_user(self.lead).get(self.url()).data["team"]
        self.assertEqual([m["id"] for m in team], [self.picked.id])

    def test_a_task_cannot_be_given_to_somebody_off_the_project(self):
        response = as_user(self.lead).post(
            self.url("/tasks"),
            {"title": "Some work", "assignee": self.bench.id,
             "amount_usd": "100"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_lead_can_add_them_mid_delivery(self):
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.bench.id]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(self.bench.id,
                      {m["id"] for m in response.data["team"]})

    def test_and_then_give_them_a_task(self):
        """The whole point — the two steps together."""
        as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.bench.id]}, format="json")
        response = as_user(self.lead).post(
            self.url("/tasks"),
            {"title": "Some work", "assignee": self.bench.id,
             "amount_usd": "100"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Task.objects.get(title="Some work").assignee_id,
                         self.bench.id)

    def test_it_works_at_every_live_stage(self):
        for stage in (Project.Stage.PAID, Project.Stage.IN_PROGRESS,
                      Project.Stage.REVIEW):
            with self.subTest(stage=stage):
                self.project.experts.set([self.picked])
                self.project.stage = stage
                self.project.save(update_fields=["stage"])
                response = as_user(self.lead).post(
                    self.url("/experts"), {"experts": [self.bench.id]},
                    format="json")
                self.assertEqual(response.status_code, 200, response.data)

    def test_adding_somebody_already_on_it_is_harmless(self):
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.picked.id]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.experts.count(), 1)

    def test_a_roster_expert_is_reachable_whatever_they_are_tagged_with(self):
        """Product lines are copied from whoever signed somebody up, not
        curated — so they must not decide who a lead can staff."""
        self.bench.product_lines.clear()
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.bench.id]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_somebody_elses_expert_still_needs_the_discipline(self):
        other_lead = User.objects.create_user(
            "liveother@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        theirs = User.objects.create_user(
            "livetheirs@ril.dev", "x", role=User.Role.EXPERT, lead=other_lead)
        theirs.product_lines.add(ProductLine.objects.get(slug="software-web"))
        response = as_user(self.lead).post(
            self.url("/experts"), {"experts": [theirs.id]}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_the_first_person_added_becomes_answerable(self):
        self.project.experts.clear()
        self.project.expert = None
        self.project.save(update_fields=["expert"])
        as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.bench.id]}, format="json")
        self.project.refresh_from_db()
        self.assertEqual(self.project.expert_id, self.bench.id)

    def test_they_are_told_they_are_on_it(self):
        mail.outbox = []
        as_user(self.lead).post(
            self.url("/experts"), {"experts": [self.bench.id]}, format="json")
        self.assertIn(self.bench.email,
                      {addr for m in mail.outbox for addr in m.to})
