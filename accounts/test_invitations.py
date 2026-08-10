"""Inviting an expert who already has an account.

"Invite" means *get this person onto my team*. It used to refuse whenever the
address already had an account — first as "Something went wrong" (the frontend
couldn't read a `many=True` error shape), then as a clear rejection that still
sent the lead looking for an admin. Neither is what they wanted, and whether an
account already exists is an implementation detail they shouldn't have to care
about.

Now an existing expert simply joins the caller's team. Only a non-expert
account is refused, because an invitation can't turn a client into an expert.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Invitation
from catalog.models import ProductLine

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def message_of(response):
    """Mirror of the frontend's errMessage() — what the lead actually reads.

    The bug was here rather than in the API: the message was always in the
    response, and the browser threw it away.
    """
    def first(node, depth=0):
        if depth > 4 or node is None:
            return None
        if isinstance(node, str):
            return node
        if isinstance(node, (list, tuple)):
            for item in node:
                found = first(item, depth + 1)
                if found:
                    return found
            return None
        if isinstance(node, dict):
            if isinstance(node.get("detail"), str):
                return node["detail"]
            for value in node.values():
                found = first(value, depth + 1)
                if found:
                    return found
        return None

    return first(response.data) or "Something went wrong."


class InviteExistingAccountTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="software-web")
        self.lead = User.objects.create_user(
            "invlead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.other_lead = User.objects.create_user(
            "invother@ril.team", "x", full_name="Other Lead",
            role=User.Role.DELIVERY_LEAD)

        self.mine = User.objects.create_user(
            "mine@ril.dev", "x", full_name="Already Mine",
            role=User.Role.EXPERT, lead=self.lead)
        self.theirs = User.objects.create_user(
            "theirs@ril.dev", "x", full_name="Someone Elses",
            role=User.Role.EXPERT, lead=self.other_lead)
        self.orphan = User.objects.create_user(
            "orphan@ril.dev", "x", full_name="No Roster",
            role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "buyer@acme.io", "x", full_name="A Buyer", role=User.Role.CLIENT)

    def invite(self, email):
        return as_user(self.lead).post(
            "/api/invitations", {"email": email}, format="json")

    def test_inviting_an_expert_from_another_team_just_works(self):
        """The point. No error, no admin — they join, and nothing is sent to an
        address that could never accept an invitation."""
        response = self.invite(self.theirs.email)
        self.assertEqual(response.status_code, 201)
        self.assertEqual([u["email"] for u in response.data["added"]],
                         [self.theirs.email])
        self.assertEqual(response.data["created"], [])
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.lead_id, self.lead.id)
        self.assertFalse(
            Invitation.objects.filter(email=self.theirs.email).exists())

    def test_re_inviting_someone_who_was_on_your_team_before_brings_them_back(self):
        was_mine = User.objects.create_user(
            "boomerang@ril.dev", "x", full_name="Came Back",
            role=User.Role.EXPERT, lead=self.other_lead)
        response = self.invite(was_mine.email)
        self.assertEqual(response.status_code, 201)
        was_mine.refresh_from_db()
        self.assertEqual(was_mine.lead_id, self.lead.id)

    def test_an_expert_on_nobodys_roster_is_picked_up(self):
        """The case a platform revamp leaves behind — an account that predates
        the roster relation and belongs to no one."""
        response = self.invite(self.orphan.email)
        self.assertEqual(response.status_code, 201)
        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.lead_id, self.lead.id)

    def test_they_gain_the_discipline_so_they_can_be_assigned(self):
        self.invite(self.orphan.email)
        self.assertIn(self.line.slug,
                      set(self.orphan.product_lines.values_list("slug", flat=True)))

    def test_someone_already_on_your_team_is_a_no_op_not_an_error(self):
        response = self.invite(self.mine.email)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["added"], [])
        self.assertEqual(response.data["skipped"],
                         [{"email": self.mine.email,
                           "reason": "already on your team"}])

    def test_a_non_expert_account_is_still_refused(self):
        """An invitation can't turn a client into an expert, and reassigning
        somebody's role from a text box isn't this form's job."""
        response = self.invite(self.customer.email)
        self.assertEqual(response.status_code, 400)
        message = message_of(response)
        self.assertIn("client", message)
        self.assertNotEqual(message, "Something went wrong.")

    def test_the_reason_reaches_the_person_reading_it(self):
        """The reported bug: a `many=True` serializer answers with a list, the
        old lookup landed on the inner object and matched no branch."""
        self.assertNotEqual(
            message_of(self.invite(self.customer.email)), "Something went wrong.")

    def test_the_email_is_matched_regardless_of_case_or_spacing(self):
        response = self.invite("  THEIRS@RIL.DEV  ")
        self.assertEqual(response.status_code, 201)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.lead_id, self.lead.id)

    # --- and inviting somebody genuinely new still works ---
    def test_a_new_email_is_invited_normally(self):
        response = self.invite("brand-new@ril.dev")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["created"]), 1)
        self.assertTrue(
            Invitation.objects.filter(email="brand-new@ril.dev").exists())

    def test_a_mixed_batch_reports_each_outcome(self):
        """A lead pastes in six addresses without knowing which already have
        accounts — the response has to account for all of them."""
        response = as_user(self.lead).post(
            "/api/invitations",
            [{"email": "fresh@ril.dev"},
             {"email": self.theirs.email},
             {"email": self.mine.email}],
            format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual([i["email"] for i in response.data["created"]],
                         ["fresh@ril.dev"])
        self.assertEqual([u["email"] for u in response.data["added"]],
                         [self.theirs.email])
        self.assertEqual([s["email"] for s in response.data["skipped"]],
                         [self.mine.email])

    def test_a_bad_address_in_a_batch_stops_all_of_it(self):
        """Validation runs over the whole batch first, so a refusal doesn't
        leave half the invitations sent and the lead unsure which."""
        response = as_user(self.lead).post(
            "/api/invitations",
            [{"email": "fresh@ril.dev"}, {"email": self.customer.email}],
            format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Invitation.objects.filter(email="fresh@ril.dev").exists())

    def test_the_previous_lead_is_told_they_moved(self):
        from django.core import mail

        mail.outbox = []
        self.invite(self.theirs.email)
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.other_lead.email, recipients)
        self.assertIn(self.theirs.email, recipients)


class ErrorShapeTests(TestCase):
    """Every DRF error shape has to survive the trip to the browser."""

    def test_the_helper_reads_each_shape(self):
        class R:
            def __init__(self, data): self.data = data

        cases = {
            "plain detail": ({"detail": "Not allowed."}, "Not allowed."),
            "serializer": ({"email": ["Bad address."]}, "Bad address."),
            "many=True": ([{"email": ["Already taken."]}], "Already taken."),
            "nested": ({"profile": {"cv": ["Too large."]}}, "Too large."),
            "bare string": ("Gone.", "Gone."),
        }
        for label, (payload, expected) in cases.items():
            self.assertEqual(message_of(R(payload)), expected, label)
