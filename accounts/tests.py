"""Partner onboarding, approval gating, and expert invitations.

Two things here are security boundaries rather than conveniences: an unapproved
partner must not be able to price client work or draw money, and an invitation
must not become a way to mint an account with someone else's email.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Invitation, PartnerProfile
from catalog.models import ProductLine
from projects.models import Project

User = get_user_model()
GOOD_PASSWORD = "sturdy-passphrase-42"


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class SignupForkTests(TestCase):
    def register(self, **extra):
        return APIClient().post("/api/auth/register", {
            "email": "someone@acme.io", "full_name": "Some One",
            "password": GOOD_PASSWORD, **extra,
        })

    def test_a_client_signup_needs_no_approval(self):
        self.assertEqual(self.register().status_code, 201)
        user = User.objects.get(email="someone@acme.io")
        self.assertEqual(user.role, User.Role.CLIENT)
        self.assertEqual(user.approval_status, User.ApprovalStatus.NOT_REQUIRED)
        self.assertTrue(user.is_approved)

    def test_a_delivery_lead_signup_starts_pending_with_a_profile(self):
        self.assertEqual(self.register(role="delivery_lead").status_code, 201)
        user = User.objects.get(email="someone@acme.io")
        self.assertEqual(user.role, User.Role.DELIVERY_LEAD)
        self.assertEqual(user.approval_status, User.ApprovalStatus.PENDING)
        self.assertFalse(user.is_approved)
        self.assertTrue(PartnerProfile.objects.filter(user=user).exists())

    def test_a_business_dev_signup_starts_pending_with_a_referral_code(self):
        self.assertEqual(self.register(role="business_dev").status_code, 201)
        user = User.objects.get(email="someone@acme.io")
        self.assertEqual(user.approval_status, User.ApprovalStatus.PENDING)
        self.assertTrue(user.referral_code.startswith("RIL-BD-"))

    def test_nobody_can_sign_themselves_up_as_an_expert(self):
        """Experts arrive by invitation — that's what makes a roster vouched for."""
        response = self.register(role="expert")
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.data)


class ApprovalGateTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="software-web")
        self.pending = User.objects.create_user(
            "pending@ril.team", "x", full_name="Pending Lead",
            role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING,
        )
        self.pending.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "c@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A brief", client=self.customer, category="Web application",
            description="…", product_line=self.line)

    def test_a_pending_lead_can_see_their_board(self):
        """They're not locked out — they just can't act on client money yet."""
        response = as_user(self.pending).get("/api/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_a_pending_lead_cannot_quote(self):
        response = as_user(self.pending).post(
            f"/api/projects/{self.project.id}/quote", {"quote_usd": 5000})
        self.assertEqual(response.status_code, 403)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.SUBMITTED)

    def test_a_pending_lead_cannot_reach_earnings(self):
        self.assertEqual(as_user(self.pending).get("/api/earnings").status_code, 403)

    def test_a_pending_lead_cannot_create_experts(self):
        response = as_user(self.pending).post("/api/users/experts", {
            "email": "new@ril.dev", "full_name": "New", "password": GOOD_PASSWORD})
        self.assertEqual(response.status_code, 403)

    def test_a_pending_lead_can_still_invite_their_team(self):
        """Their team is part of what's being reviewed, so this stays open."""
        response = as_user(self.pending).post("/api/invitations", {
            "email": "invitee@ril.dev", "full_name": "An Invitee"})
        self.assertEqual(response.status_code, 201)

    def test_approval_unlocks_quoting(self):
        self.pending.approve()
        response = as_user(self.pending).post(
            f"/api/projects/{self.project.id}/quote", {"quote_usd": 5000})
        self.assertEqual(response.status_code, 200)

    def test_a_rejected_lead_stays_blocked(self):
        self.pending.reject(reason="Not enough delivery history.")
        response = as_user(self.pending).post(
            f"/api/projects/{self.project.id}/quote", {"quote_usd": 5000})
        self.assertEqual(response.status_code, 403)

    def test_a_lead_created_outside_the_signup_flow_works_immediately(self):
        """The seed, the shell and the Django admin never set an approval status
        — those leads must not be silently crippled."""
        direct = User.objects.create_user(
            "direct@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        direct.product_lines.add(self.line)
        self.assertEqual(direct.approval_status, User.ApprovalStatus.NOT_REQUIRED)
        self.assertTrue(direct.is_approved)
        response = as_user(direct).post(
            f"/api/projects/{self.project.id}/quote", {"quote_usd": 5000})
        self.assertEqual(response.status_code, 200)


class OnboardingTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "lead@ril.team", "x", role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING)

    def test_progress_is_saved_and_resumable(self):
        client = as_user(self.lead)
        client.patch("/api/onboarding", {
            "full_name": "Ada Lead", "onboarding_step": 2,
            "profile": {"country": "Nigeria", "bio": "Ten years in design."},
        }, format="json")

        response = client.get("/api/onboarding")
        self.assertEqual(response.data["user"]["full_name"], "Ada Lead")
        self.assertEqual(response.data["user"]["onboarding_step"], 2)
        self.assertEqual(response.data["profile"]["country"], "Nigeria")

    def test_the_step_cursor_never_moves_backwards(self):
        """Going back to fix step 2 must not undo that they reached step 5."""
        client = as_user(self.lead)
        client.patch("/api/onboarding", {"onboarding_step": 5}, format="json")
        client.patch("/api/onboarding", {"onboarding_step": 2}, format="json")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.onboarding_step, 5)

    def test_product_lines_are_set_from_slugs(self):
        as_user(self.lead).patch(
            "/api/onboarding", {"product_lines": ["design-creative"]}, format="json")
        self.assertEqual(list(self.lead.product_lines.all()), [self.line])

    def test_submitting_an_incomplete_application_is_refused_with_specifics(self):
        response = as_user(self.lead).post("/api/onboarding/submit")
        self.assertEqual(response.status_code, 400)
        self.assertIn("missing", response.data)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.approval_status, User.ApprovalStatus.PENDING)
        self.assertIsNone(self.lead.applied_at)

    def test_a_complete_application_can_be_submitted(self):
        client = as_user(self.lead)
        client.patch("/api/onboarding", {
            "full_name": "Ada Lead",
            "product_lines": ["design-creative"],
            "profile": {"country": "Nigeria", "past_delivery": "40 brand projects."},
        }, format="json")
        response = client.post("/api/onboarding/submit")
        self.assertEqual(response.status_code, 200)
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.applied_at)
        self.assertIsNotNone(self.lead.onboarding_completed_at)

    def test_a_client_has_no_onboarding(self):
        customer = User.objects.create_user("c2@acme.io", "x", role=User.Role.CLIENT)
        self.assertEqual(as_user(customer).get("/api/onboarding").status_code, 403)


class ApplicationReviewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin@ril.team", "x")
        self.applicant = User.objects.create_user(
            "applicant@ril.team", "x", full_name="An Applicant",
            role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING,
            applied_at=timezone.now())
        PartnerProfile.objects.create(user=self.applicant, country="Kenya")

    def test_the_queue_lists_pending_applications_with_their_profile(self):
        response = as_user(self.admin).get("/api/applications")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["profile"]["country"], "Kenya")

    def test_approving_clears_the_account(self):
        response = as_user(self.admin).post(
            f"/api/applications/{self.applicant.id}/decide", {"decision": "approve"})
        self.assertEqual(response.status_code, 200)
        self.applicant.refresh_from_db()
        self.assertTrue(self.applicant.is_approved)
        self.assertEqual(self.applicant.approved_by, self.admin)

    def test_rejecting_records_the_reason_for_the_applicant(self):
        as_user(self.admin).post(
            f"/api/applications/{self.applicant.id}/decide",
            {"decision": "reject", "reason": "Not enough delivery history."})
        self.applicant.refresh_from_db()
        self.assertFalse(self.applicant.is_approved)
        self.assertEqual(self.applicant.rejection_reason,
                         "Not enough delivery history.")

    def test_a_non_admin_cannot_decide(self):
        other = User.objects.create_user(
            "other@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        response = as_user(other).post(
            f"/api/applications/{self.applicant.id}/decide", {"decision": "approve"})
        self.assertEqual(response.status_code, 403)
        self.applicant.refresh_from_db()
        self.assertFalse(self.applicant.is_approved)


class InvitationTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "lead@ril.team", "x", full_name="A Lead", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)

    def invite(self, payload=None, **kw):
        return as_user(self.lead).post(
            "/api/invitations",
            payload if payload is not None else {"email": "invitee@ril.dev", **kw},
            format="json")

    def test_a_lead_invites_an_expert(self):
        response = self.invite(full_name="An Invitee", product_lines=["design-creative"])
        self.assertEqual(response.status_code, 201)
        invitation = Invitation.objects.get(email="invitee@ril.dev")
        self.assertEqual(invitation.invited_by, self.lead)
        self.assertEqual(list(invitation.product_lines.all()), [self.line])

    def test_a_batch_of_invitations_is_sent_in_one_call(self):
        response = self.invite([
            {"email": "a@ril.dev"}, {"email": "b@ril.dev"}, {"email": "c@ril.dev"},
        ])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["created"]), 3)

    def test_lines_default_to_the_leads_own_when_unspecified(self):
        """An expert must never be created unassignable."""
        self.invite()
        self.assertEqual(
            list(Invitation.objects.get(email="invitee@ril.dev").product_lines.all()),
            [self.line])

    def test_inviting_an_existing_account_is_refused(self):
        User.objects.create_user("taken@ril.dev", "x", role=User.Role.EXPERT)
        response = self.invite({"email": "taken@ril.dev"})
        self.assertEqual(response.status_code, 400)

    def test_a_duplicate_pending_invitation_is_skipped_not_duplicated(self):
        self.invite()
        response = self.invite()
        self.assertEqual(response.data["skipped"], ["invitee@ril.dev"])
        self.assertEqual(Invitation.objects.filter(email="invitee@ril.dev").count(), 1)

    def test_accepting_creates_an_expert_on_the_leads_roster(self):
        self.invite(full_name="An Invitee", product_lines=["design-creative"])
        token = Invitation.objects.get(email="invitee@ril.dev").token

        response = APIClient().post(f"/api/invite/{token}/accept",
                                    {"password": GOOD_PASSWORD})
        self.assertEqual(response.status_code, 201)
        expert = User.objects.get(email="invitee@ril.dev")
        self.assertEqual(expert.role, User.Role.EXPERT)
        self.assertEqual(expert.lead, self.lead)
        self.assertEqual(list(expert.product_lines.all()), [self.line])
        # Accepting proves they own the address, so no second verification step.
        self.assertTrue(expert.is_email_verified)
        # They chose the password, so they can actually sign in with it.
        self.assertTrue(expert.check_password(GOOD_PASSWORD))
        self.assertIn("access", response.data)

    def test_an_invitation_can_only_be_accepted_once(self):
        self.invite()
        token = Invitation.objects.get(email="invitee@ril.dev").token
        APIClient().post(f"/api/invite/{token}/accept", {"password": GOOD_PASSWORD})
        response = APIClient().post(f"/api/invite/{token}/accept",
                                    {"password": GOOD_PASSWORD})
        self.assertEqual(response.status_code, 410)

    def test_an_expired_invitation_is_refused(self):
        self.invite()
        invitation = Invitation.objects.get(email="invitee@ril.dev")
        invitation.expires_at = timezone.now() - timezone.timedelta(days=1)
        invitation.save()
        response = APIClient().post(f"/api/invite/{invitation.token}/accept",
                                    {"password": GOOD_PASSWORD})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(User.objects.filter(email="invitee@ril.dev").exists())

    def test_a_revoked_invitation_is_refused(self):
        self.invite()
        invitation = Invitation.objects.get(email="invitee@ril.dev")
        as_user(self.lead).post(f"/api/invitations/{invitation.id}/revoke")
        response = APIClient().post(f"/api/invite/{invitation.token}/accept",
                                    {"password": GOOD_PASSWORD})
        self.assertEqual(response.status_code, 410)

    def test_a_weak_password_is_rejected(self):
        self.invite()
        token = Invitation.objects.get(email="invitee@ril.dev").token
        response = APIClient().post(f"/api/invite/{token}/accept", {"password": "abc"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="invitee@ril.dev").exists())

    def test_an_unknown_token_reveals_nothing(self):
        import uuid
        response = APIClient().get(f"/api/invite/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)

    def test_a_lead_cannot_revoke_someone_elses_invitation(self):
        self.invite()
        invitation = Invitation.objects.get(email="invitee@ril.dev")
        other = User.objects.create_user(
            "other@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        response = as_user(other).post(f"/api/invitations/{invitation.id}/revoke")
        self.assertEqual(response.status_code, 404)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, Invitation.Status.PENDING)

    def test_resending_renews_the_expiry(self):
        """A resent link that's still expired is a dead end that looks live."""
        self.invite()
        invitation = Invitation.objects.get(email="invitee@ril.dev")
        invitation.expires_at = timezone.now() - timezone.timedelta(days=1)
        invitation.save()
        as_user(self.lead).post(f"/api/invitations/{invitation.id}/resend")
        invitation.refresh_from_db()
        self.assertFalse(invitation.is_expired)
