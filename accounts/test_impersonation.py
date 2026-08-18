"""An admin signing in as another user.

The feature is one endpoint and a claim; nearly all of the risk is in the edges,
so that's where the tests are — who may start one, what the borrowed token can
and can't do, and whether it can quietly stop looking borrowed.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import ImpersonationEvent

User = get_user_model()


def bearer(access):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


class ImpersonationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            "boss@ril.team", "x", full_name="The Admin")
        self.client_user = User.objects.create_user(
            "buyer@acme.io", "x", full_name="A Buyer",
            role=User.Role.CLIENT, is_email_verified=True)
        self.lead = User.objects.create_user(
            "lead@ril.team", "x", full_name="A Lead",
            role=User.Role.DELIVERY_LEAD, is_email_verified=True)
        # Staff is not admin: they see the books, which is a different power
        # from wearing somebody's face.
        self.staffer = User.objects.create_user(
            "staff@ril.team", "x", full_name="A Staffer",
            role=User.Role.DELIVERY_LEAD, is_staff=True, is_email_verified=True)

    def sign_in(self, user):
        response = APIClient().post(
            "/api/auth/login", {"email": user.email, "password": "x"})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def start(self, target, as_user=None, reason="checking a bug"):
        me = self.sign_in(as_user or self.admin)
        return bearer(me["access"]).post(
            f"/api/users/{target.id}/impersonate", {"reason": reason})

    # --- who may start one ---
    def test_an_admin_can_sign_in_as_any_user(self):
        for target in (self.client_user, self.lead, self.staffer):
            with self.subTest(target=target.email):
                response = self.start(target)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["user"]["email"], target.email)

    def test_the_borrowed_token_really_is_the_target(self):
        tokens = self.start(self.client_user).data
        me = bearer(tokens["access"]).get("/api/auth/me")
        self.assertEqual(me.data["email"], self.client_user.email)
        self.assertEqual(me.data["role"], User.Role.CLIENT)

    def test_a_delivery_lead_cannot_impersonate(self):
        self.assertEqual(self.start(self.client_user, as_user=self.lead).status_code, 403)

    def test_staff_alone_is_not_enough(self):
        """`is_staff` opens the books, not other people's accounts."""
        self.assertEqual(
            self.start(self.client_user, as_user=self.staffer).status_code, 403)

    def test_an_unknown_user_is_a_404(self):
        me = self.sign_in(self.admin)
        self.assertEqual(
            bearer(me["access"]).post("/api/users/999999/impersonate").status_code, 404)

    def test_impersonating_yourself_is_refused(self):
        self.assertEqual(self.start(self.admin).status_code, 400)

    def test_sessions_cannot_be_nested(self):
        """Otherwise "who is really doing this?" becomes a chain to unwind.

        Two different gates stop it, depending on who you borrowed. Inside an
        ordinary user you simply aren't an admin any more, so the admin check
        refuses first; inside another admin you are, and the explicit nesting
        check is the one that catches it.
        """
        inside_a_client = self.start(self.client_user).data
        self.assertEqual(
            bearer(inside_a_client["access"])
            .post(f"/api/users/{self.lead.id}/impersonate").status_code, 403)

        second_admin = User.objects.create_superuser("boss2@ril.team", "x")
        inside_an_admin = self.start(second_admin).data
        self.assertEqual(
            bearer(inside_an_admin["access"])
            .post(f"/api/users/{self.lead.id}/impersonate").status_code, 400)

    # --- the banner's data ---
    def test_me_names_the_admin_behind_the_session(self):
        tokens = self.start(self.client_user).data
        me = bearer(tokens["access"]).get("/api/auth/me")
        self.assertEqual(me.data["impersonated_by"]["email"], self.admin.email)

    def test_an_ordinary_session_is_not_marked_as_borrowed(self):
        me = bearer(self.sign_in(self.lead)["access"]).get("/api/auth/me")
        self.assertIsNone(me.data["impersonated_by"])

    # --- getting back out ---
    def test_stopping_returns_the_admin_to_themselves(self):
        tokens = self.start(self.client_user).data
        response = bearer(tokens["access"]).post("/api/impersonation/stop")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["user"]["email"], self.admin.email)
        me = bearer(response.data["access"]).get("/api/auth/me")
        self.assertEqual(me.data["email"], self.admin.email)
        self.assertIsNone(me.data["impersonated_by"])

    def test_stopping_when_you_are_yourself_is_refused(self):
        me = self.sign_in(self.lead)
        self.assertEqual(
            bearer(me["access"]).post("/api/impersonation/stop").status_code, 400)

    def test_the_way_back_is_not_taken_from_the_request(self):
        """The admin comes from the signed claim, so a caller can't nominate one."""
        tokens = self.start(self.client_user).data
        response = bearer(tokens["access"]).post(
            "/api/impersonation/stop", {"user_id": self.staffer.id})
        self.assertEqual(response.data["user"]["email"], self.admin.email)

    def test_an_admin_stripped_of_their_rights_cannot_be_returned_to(self):
        tokens = self.start(self.client_user).data
        self.admin.is_superuser = False
        self.admin.save(update_fields=["is_superuser"])
        self.assertEqual(
            bearer(tokens["access"]).post("/api/impersonation/stop").status_code, 403)

    # --- the claim can't be shaken off ---
    def test_refreshing_keeps_the_session_marked_as_borrowed(self):
        """The one way a borrowed token could launder itself into an ordinary
        one is by outliving its access token — so the claim rides the refresh."""
        tokens = self.start(self.client_user).data
        refreshed = APIClient().post(
            "/api/auth/token/refresh", {"refresh": tokens["refresh"]})
        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        me = bearer(refreshed.data["access"]).get("/api/auth/me")
        self.assertEqual(me.data["email"], self.client_user.email)
        self.assertEqual(me.data["impersonated_by"]["email"], self.admin.email)

    # --- what a borrowed session may not do ---
    def test_it_cannot_change_the_password(self):
        """Knowing the old password is what proves it's really them — and it
        would lock the owner out of their own account."""
        tokens = self.start(self.client_user).data
        response = bearer(tokens["access"]).post(
            "/api/auth/change-password",
            {"old_password": "x", "new_password": "Str0ng-New-Pass!23"})
        self.assertEqual(response.status_code, 403)
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.check_password("x"))

    def test_it_cannot_repoint_a_payout_account(self):
        """Reading it is most of what support needs; changing where the money
        lands, from inside their account, is indistinguishable from fraud."""
        expert = User.objects.create_user(
            "earner@ril.dev", "x", role=User.Role.EXPERT, is_email_verified=True)
        tokens = self.start(expert).data
        response = bearer(tokens["access"]).put(
            "/api/payouts/account",
            {"bank_name": "Some Bank", "bank_code": "058",
             "bank_account_number": "0123456789",
             "bank_account_name": "Not Them"},
            format="json")
        self.assertEqual(response.status_code, 403)
        expert.refresh_from_db()
        self.assertEqual(expert.bank_account_number, "")

    def test_reading_a_payout_account_is_still_allowed(self):
        expert = User.objects.create_user(
            "reader@ril.dev", "x", role=User.Role.EXPERT, is_email_verified=True)
        tokens = self.start(expert).data
        self.assertEqual(
            bearer(tokens["access"]).get("/api/payouts/account").status_code, 200)

    def test_the_owner_can_still_change_their_own_password(self):
        me = self.sign_in(self.client_user)
        response = bearer(me["access"]).post(
            "/api/auth/change-password",
            {"old_password": "x", "new_password": "Str0ng-New-Pass!23"})
        self.assertEqual(response.status_code, 200, response.data)

    # --- the audit trail ---
    def test_a_row_is_written_with_the_reason(self):
        self.start(self.client_user, reason="their assign page is empty")
        event = ImpersonationEvent.objects.get()
        self.assertEqual(event.impersonator_id, self.admin.id)
        self.assertEqual(event.target_id, self.client_user.id)
        self.assertEqual(event.reason, "their assign page is empty")
        self.assertIsNone(event.ended_at)

    def test_stopping_closes_the_row(self):
        tokens = self.start(self.client_user).data
        bearer(tokens["access"]).post("/api/impersonation/stop")
        self.assertIsNotNone(ImpersonationEvent.objects.get().ended_at)

    def test_a_refused_attempt_leaves_no_row(self):
        self.start(self.client_user, as_user=self.lead)
        self.assertEqual(ImpersonationEvent.objects.count(), 0)

    def test_only_an_admin_reads_the_log(self):
        self.start(self.client_user)
        self.assertEqual(
            bearer(self.sign_in(self.lead)["access"])
            .get("/api/impersonation/log").status_code, 403)
        allowed = bearer(self.sign_in(self.admin)["access"]).get("/api/impersonation/log")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(len(allowed.data), 1)


class DirectoryTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("dboss@ril.team", "x")
        self.someone = User.objects.create_user(
            "findme@acme.io", "x", full_name="Find Me",
            role=User.Role.CLIENT, is_email_verified=True)

    def as_admin(self):
        me = APIClient().post(
            "/api/auth/login", {"email": self.admin.email, "password": "x"}).data
        return bearer(me["access"])

    def test_an_admin_searches_every_role_not_just_experts(self):
        rows = self.as_admin().get("/api/users", {"q": "findme"}).data
        self.assertEqual([r["email"] for r in rows], ["findme@acme.io"])

    def test_it_matches_on_name_too(self):
        rows = self.as_admin().get("/api/users", {"q": "Find Me"}).data
        self.assertEqual(len(rows), 1)

    def test_it_is_closed_to_everyone_else(self):
        me = APIClient().post(
            "/api/auth/login", {"email": self.someone.email, "password": "x"}).data
        self.assertEqual(bearer(me["access"]).get("/api/users").status_code, 403)


class DirectoryScopeTests(TestCase):
    """A delivery lead needs a client to set a retainer up for. That is not a
    reason to hand them the impersonation directory."""

    def setUp(self):
        self.admin = User.objects.create_superuser("dsboss@ril.team", "x")
        self.lead = User.objects.create_user(
            "dslead@ril.team", "x", role=User.Role.DELIVERY_LEAD,
            is_email_verified=True)
        self.buyer = User.objects.create_user(
            "dsbuyer@acme.io", "x", full_name="A Buyer",
            role=User.Role.CLIENT, is_email_verified=True)
        self.expert = User.objects.create_user(
            "dsexpert@ril.dev", "x", role=User.Role.EXPERT,
            is_email_verified=True)

    def signed_in(self, user):
        me = APIClient().post(
            "/api/auth/login", {"email": user.email, "password": "x"}).data
        return bearer(me["access"])

    def test_a_lead_sees_clients_only(self):
        rows = self.signed_in(self.lead).get("/api/users").data
        self.assertEqual({r["email"] for r in rows}, {self.buyer.email})

    def test_a_lead_cannot_widen_it_with_a_role_filter(self):
        """The parameter is ignored for a lead, not honoured — they see clients
        whatever they ask for, and never another lead's experts."""
        rows = self.signed_in(self.lead).get("/api/users", {"role": "expert"}).data
        self.assertEqual({r["role"] for r in rows}, {"client"})
        self.assertNotIn(self.expert.email, {r["email"] for r in rows})

    def test_an_admin_still_sees_everyone(self):
        rows = self.signed_in(self.admin).get("/api/users").data
        self.assertGreaterEqual(len(rows), 4)

    def test_a_client_still_sees_nothing(self):
        self.assertEqual(
            self.signed_in(self.buyer).get("/api/users").status_code, 403)

    def test_an_expert_still_sees_nothing(self):
        self.assertEqual(
            self.signed_in(self.expert).get("/api/users").status_code, 403)
