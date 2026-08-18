"""The business developer commission.

The commission comes out of the same quote everyone else is paid from, so the
rules that matter are: it is only charged when a business developer is actually
attributed, the split still closes exactly, and attribution can't move after the
client has paid.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import SiteSettings
from catalog.models import ProductLine
from payments import earnings as earnings_service
from payments.models import Earning
from projects.models import Project

User = get_user_model()


class CommissionTestCase(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="software-web")
        self.lead = User.objects.create_user(
            "lead@ril.team", "x", full_name="A Lead", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "expert@ril.dev", "x", full_name="An Expert", role=User.Role.EXPERT)
        self.expert.product_lines.add(self.line)
        self.bizdev = User.objects.create_user(
            "bd@ril.team", "x", full_name="A Bizdev", role=User.Role.BUSINESS_DEV)
        self.customer = User.objects.create_user(
            "c@acme.io", "x", full_name="A Client", role=User.Role.CLIENT)

    def project(self, quote=10000, bizdev=None, **kwargs):
        return Project.objects.create(
            title="A brief", client=self.customer, category="Web application",
            description="…", quote_usd=quote, lead=self.lead, expert=self.expert,
            product_line=self.line, business_developer=bizdev, **kwargs,
        )

    def assert_closes(self, split):
        total = (split["expert_usd"] + split["delivery_lead_usd"]
                 + split["business_dev_usd"] + split["platform_usd"])
        self.assertEqual(total, split["quote_usd"])


class CommissionSplitTests(CommissionTestCase):
    def test_a_direct_project_pays_no_commission_and_the_platform_keeps_it(self):
        split = self.project(quote=10000).payout_split()
        self.assertEqual(split["business_dev_usd"], Decimal("0.00"))
        self.assertEqual(split["business_dev_percent"], Decimal("0"))
        self.assertFalse(split["has_business_dev"])
        self.assertEqual(split["platform_usd"], Decimal("1500.00"))  # keeps the 5%
        self.assert_closes(split)

    def test_a_sourced_project_pays_5_percent_out_of_the_platform_share(self):
        split = self.project(quote=10000, bizdev=self.bizdev).payout_split()
        self.assertEqual(split["expert_usd"], Decimal("7000.00"))
        self.assertEqual(split["delivery_lead_usd"], Decimal("1500.00"))
        self.assertEqual(split["business_dev_usd"], Decimal("500.00"))
        self.assertEqual(split["platform_usd"], Decimal("1000.00"))
        self.assertTrue(split["has_business_dev"])
        self.assert_closes(split)

    def test_a_per_project_commission_override_is_honoured(self):
        split = self.project(
            quote=10000, bizdev=self.bizdev,
            business_dev_share_percent=Decimal("10"),
        ).payout_split()
        self.assertEqual(split["business_dev_usd"], Decimal("1000.00"))
        self.assertEqual(split["platform_usd"], Decimal("500.00"))
        self.assertTrue(split["uses_override"])
        self.assert_closes(split)

    def test_an_override_without_an_attributed_bizdev_still_pays_nothing(self):
        """The override sets the rate, not the entitlement."""
        split = self.project(
            quote=10000, business_dev_share_percent=Decimal("10")
        ).payout_split()
        self.assertEqual(split["business_dev_usd"], Decimal("0.00"))
        self.assert_closes(split)

    def test_the_split_closes_when_the_commission_does_not_divide_evenly(self):
        self.assert_closes(self.project(quote=3333, bizdev=self.bizdev).payout_split())

    def test_shares_including_the_commission_cannot_exceed_100_percent(self):
        from django.core.exceptions import ValidationError

        project = self.project(
            quote=10000, bizdev=self.bizdev,
            expert_share_percent=Decimal("70"),
            delivery_lead_share_percent=Decimal("25"),
            business_dev_share_percent=Decimal("10"),  # 105%
        )
        with self.assertRaises(ValidationError):
            project.full_clean()


class CommissionCreditTests(CommissionTestCase):
    def test_the_commission_is_credited_on_approval(self):
        project = self.project(quote=10000, bizdev=self.bizdev,
                               stage=Project.Stage.COMPLETED)
        earnings_service.record_project_earnings(project)
        earning = Earning.objects.get(user=self.bizdev, kind=Earning.Kind.BUSINESS_DEV)
        self.assertEqual(earning.amount_usd, Decimal("500.00"))
        self.assertEqual(earning.share_percent, Decimal("5.00"))

    def test_a_bizdev_can_withdraw_their_commission(self):
        project = self.project(quote=10000, bizdev=self.bizdev,
                               stage=Project.Stage.COMPLETED)
        earnings_service.record_project_earnings(project)
        summary = earnings_service.summary(self.bizdev)
        self.assertEqual(summary["earned_usd"], Decimal("500.00"))
        self.assertEqual(summary["available_usd"], Decimal("500.00"))
        self.assertTrue(self.bizdev.can_earn)

    def test_pending_commission_shows_before_approval(self):
        self.project(quote=10000, bizdev=self.bizdev, stage=Project.Stage.IN_PROGRESS)
        summary = earnings_service.summary(self.bizdev)
        self.assertEqual(summary["earned_usd"], Decimal("0.00"))
        self.assertEqual(summary["pending_usd"], Decimal("500.00"))
        self.assertEqual(summary["available_usd"], Decimal("0.00"))

    def test_a_settled_project_reports_the_credited_commission(self):
        project = self.project(quote=10000, bizdev=self.bizdev,
                               stage=Project.Stage.COMPLETED)
        earnings_service.record_project_earnings(project)
        # The rate is raised afterwards; the credited amount must not move.
        row = SiteSettings.load()
        row.business_dev_share_percent = Decimal("20")
        row.save()
        split = project.payout_split()
        self.assertEqual(split["business_dev_usd"], Decimal("500.00"))
        self.assert_closes(split)


class AttributionTests(CommissionTestCase):
    def as_user(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_a_referred_client_posts_an_attributed_brief(self):
        self.customer.referred_by = self.bizdev
        self.customer.save()
        response = self.as_user(self.customer).post("/api/projects", {
            "title": "A build", "product_line": "software-web",
            "description": "Something useful.",
        })
        self.assertEqual(response.status_code, 201)
        project = Project.objects.get(id=response.data["id"])
        self.assertEqual(project.business_developer, self.bizdev)

    def test_an_unreferred_client_posts_a_direct_brief(self):
        response = self.as_user(self.customer).post("/api/projects", {
            "title": "A build", "product_line": "software-web",
            "description": "Something useful.",
        })
        project = Project.objects.get(id=response.data["id"])
        self.assertIsNone(project.business_developer)

    def test_every_project_from_a_referred_client_is_attributed(self):
        """Attribution is per-client and ongoing, not first-project-only."""
        self.customer.referred_by = self.bizdev
        self.customer.save()
        for _ in range(2):
            self.as_user(self.customer).post("/api/projects", {
                "title": "A build", "product_line": "software-web",
                "description": "Something useful.",
            })
        self.assertEqual(
            Project.objects.filter(business_developer=self.bizdev).count(), 2)

    def test_a_lead_can_correct_attribution_before_payment(self):
        project = self.project(quote=5000, stage=Project.Stage.QUOTED)
        response = self.as_user(self.lead).post(
            f"/api/projects/{project.id}/attribute",
            {"business_developer": self.bizdev.id})
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.business_developer, self.bizdev)
        self.assertTrue(project.activity.filter(text__contains="Credited").exists())

    def test_attribution_is_locked_once_the_client_has_paid(self):
        """The commission comes out of money that has already changed hands."""
        project = self.project(quote=5000, stage=Project.Stage.PAID)
        response = self.as_user(self.lead).post(
            f"/api/projects/{project.id}/attribute",
            {"business_developer": self.bizdev.id})
        self.assertEqual(response.status_code, 400)
        project.refresh_from_db()
        self.assertIsNone(project.business_developer)

    def test_a_client_cannot_attribute_a_project(self):
        project = self.project(quote=5000, stage=Project.Stage.QUOTED)
        response = self.as_user(self.customer).post(
            f"/api/projects/{project.id}/attribute",
            {"business_developer": self.bizdev.id})
        self.assertIn(response.status_code, (403, 404))

    def test_only_a_business_developer_can_be_attributed(self):
        project = self.project(quote=5000, stage=Project.Stage.QUOTED)
        response = self.as_user(self.lead).post(
            f"/api/projects/{project.id}/attribute",
            {"business_developer": self.expert.id})
        self.assertEqual(response.status_code, 400)


class ReferralSignupTests(TestCase):
    def test_signing_up_with_a_referral_code_links_the_client(self):
        bizdev = User.objects.create_user(
            "bd2@ril.team", "x", role=User.Role.BUSINESS_DEV)
        code = bizdev.ensure_referral_code()
        self.assertTrue(code.startswith("RIL-BD-"))

        response = APIClient().post("/api/auth/register", {
            "email": "new@acme.io", "full_name": "New Client",
            "password": "sturdy-passphrase-42", "referral_code": code.lower(),
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(User.objects.get(email="new@acme.io").referred_by, bizdev)

    def test_an_unknown_referral_code_is_ignored_not_rejected(self):
        """A stale or mistyped link must never block a signup."""
        response = APIClient().post("/api/auth/register", {
            "email": "new2@acme.io", "full_name": "New Client",
            "password": "sturdy-passphrase-42", "referral_code": "RIL-BD-ZZZZ",
        })
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(User.objects.get(email="new2@acme.io").referred_by)

    def test_a_referral_code_is_only_issued_to_business_developers(self):
        client_user = User.objects.create_user(
            "c2@acme.io", "x", role=User.Role.CLIENT)
        self.assertEqual(client_user.ensure_referral_code(), "")

    def test_issuing_a_code_twice_keeps_the_first(self):
        bizdev = User.objects.create_user(
            "bd3@ril.team", "x", role=User.Role.BUSINESS_DEV)
        first = bizdev.ensure_referral_code()
        self.assertEqual(bizdev.ensure_referral_code(), first)
