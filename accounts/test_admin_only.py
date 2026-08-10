"""Screens that belong to an admin, not to the delivery_lead role.

The same trap as the project-scoping bug, in a different place: `create_superuser`
gives admins the delivery_lead role, so `role == "delivery_lead"` reads like an
admin check and behaves like one right up until an ordinary lead signs in.

Applications leaked because of it. Verifications only showed a dead menu entry,
but both are pinned here so neither drifts back.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import KycProfile

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class AdminOnlyQueueTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("queueadmin@ril.team", "x")
        self.lead = User.objects.create_user(
            "queuelead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "queueexpert@ril.dev", "x", role=User.Role.EXPERT)
        # Somebody waiting to be vetted, with a real application behind them.
        self.applicant = User.objects.create_user(
            "applicant@ril.team", "x", full_name="Hopeful Applicant",
            role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING)
        KycProfile.objects.create(user=self.expert,
                                  status=KycProfile.Status.PENDING)

    def test_an_ordinary_lead_cannot_read_the_applications_queue(self):
        """The leak. A pending application is somebody's CV and profile,
        submitted to the platform — not to a peer who can't act on it."""
        response = as_user(self.lead).get("/api/applications")
        self.assertEqual(response.status_code, 403)

    def test_an_admin_still_reviews_applications(self):
        response = as_user(self.admin).get("/api/applications")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["email"] for row in response.data],
                         [self.applicant.email])

    def test_reading_and_deciding_agree_on_who_may_do_it(self):
        """They disagreed: a lead could see the queue but never act on it."""
        listed = as_user(self.lead).get("/api/applications").status_code
        decided = as_user(self.lead).post(
            f"/api/applications/{self.applicant.id}/decide",
            {"decision": "approve"}, format="json").status_code
        self.assertEqual(listed, decided, "one gate is looser than the other")
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.approval_status,
                         User.ApprovalStatus.PENDING)

    def test_an_ordinary_lead_cannot_read_the_identity_queue(self):
        self.assertEqual(as_user(self.lead).get("/api/verifications").status_code, 403)

    def test_an_admin_still_reviews_identity_documents(self):
        response = as_user(self.admin).get("/api/verifications")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)


class AdminFlagTests(TestCase):
    """`is_admin` on the user payload — the only way the frontend can tell an
    admin from a lead, since both carry the same role."""

    def test_an_admin_is_flagged(self):
        admin = User.objects.create_superuser("flagadmin@ril.team", "x")
        data = as_user(admin).get("/api/auth/me").data
        self.assertTrue(data["is_admin"])
        self.assertEqual(data["role"], User.Role.DELIVERY_LEAD,
                         "an admin carries the lead role — that's the whole trap")

    def test_an_ordinary_lead_is_not(self):
        lead = User.objects.create_user(
            "flaglead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.assertFalse(as_user(lead).get("/api/auth/me").data["is_admin"])

    def test_it_cannot_be_granted_by_editing_your_own_profile(self):
        lead = User.objects.create_user(
            "climber@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        response = as_user(lead).patch(
            "/api/auth/me", {"is_admin": True, "full_name": "Climber"}, format="json")
        self.assertIn(response.status_code, (200, 400))
        lead.refresh_from_db()
        self.assertFalse(lead.is_superuser)
