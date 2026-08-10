"""Inviting an expert who already has an account.

An invitation creates an account, so it can't target one that exists. That's
correct and stays. What was wrong is that the rejection led nowhere: the lead
saw "Something went wrong" — the frontend couldn't read a `many=True` error
shape — and the only readable version, "Someone with this email already has an
account", still didn't say whether the person was already theirs to assign.
Both roads led back to inviting again, which can never work.
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

    def test_the_reason_reaches_the_person_reading_it(self):
        """The reported bug. A `many=True` serializer answers with a list, the
        old lookup landed on the inner object and matched no branch, and a
        precise rejection arrived as "Something went wrong"."""
        response = self.invite(self.mine.email)
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(message_of(response), "Something went wrong.")

    def test_someone_already_on_your_roster_says_so(self):
        message = message_of(self.invite(self.mine.email))
        self.assertIn("Already Mine", message)
        self.assertIn("already on your team", message)

    def test_someone_on_another_leads_roster_says_whose(self):
        message = message_of(self.invite(self.theirs.email))
        self.assertIn("Other Lead", message)

    def test_an_expert_on_nobodys_roster_is_distinguished(self):
        """The case a platform revamp leaves behind — an account that predates
        the roster relation and belongs to no one."""
        message = message_of(self.invite(self.orphan.email))
        self.assertIn("isn't on anyone's team", message)

    def test_a_non_expert_account_says_what_it_is(self):
        message = message_of(self.invite(self.customer.email))
        self.assertIn("client", message)

    def test_the_email_is_matched_regardless_of_case_or_spacing(self):
        response = self.invite("  MINE@RIL.DEV  ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already on your team", message_of(response))

    # --- and inviting somebody genuinely new still works ---
    def test_a_new_email_is_invited_normally(self):
        response = self.invite("brand-new@ril.dev")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["created"]), 1)
        self.assertTrue(
            Invitation.objects.filter(email="brand-new@ril.dev").exists())

    def test_one_bad_address_does_not_lose_the_batch(self):
        """A lead invites six people at once; the response has to say which one
        was the problem rather than failing opaquely."""
        response = as_user(self.lead).post(
            "/api/invitations",
            [{"email": "fresh@ril.dev"}, {"email": self.mine.email}],
            format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already on your team", message_of(response))
        # Nothing was sent — the batch is validated before any of it is written.
        self.assertFalse(Invitation.objects.filter(email="fresh@ril.dev").exists())


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
