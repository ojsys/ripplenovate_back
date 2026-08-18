"""The revenue split, pinned as policy rather than left to a settings row.

    Expert            70%
    Delivery lead     15%
    Business developer 5%   — only when one sourced the project
    Platform          the remainder: 15% direct, 10% sourced

The platform's cut is deliberately never stored. It is whatever the other three
don't claim, which is what makes "the shares always add up to the quote" an
invariant rather than a thing somebody has to remember to check.

These tests exist because the numbers are a business decision that lives in a
database row an admin can edit. Without them, a stray edit is indistinguishable
from a deliberate change, and nothing would fail.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import SiteSettings
from catalog.models import ProductLine
from projects.models import Project

User = get_user_model()


class SharePolicyTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "sp-lead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "sp-expert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.bizdev = User.objects.create_user(
            "sp-bd@ril.team", "x", role=User.Role.BUSINESS_DEV)
        self.customer = User.objects.create_user(
            "sp-client@acme.io", "x", role=User.Role.CLIENT)

    def project(self, *, with_bd=False, quote=10000):
        return Project.objects.create(
            title="A brief", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert,
            business_developer=self.bizdev if with_bd else None,
            stage=Project.Stage.IN_PROGRESS, quote_usd=quote)

    # --- the configured defaults ---
    def test_the_shipped_defaults_are_the_policy(self):
        cfg = SiteSettings.payout_config()
        self.assertEqual(Decimal(cfg["expert_share_percent"]), Decimal("70.00"))
        self.assertEqual(Decimal(cfg["delivery_lead_share_percent"]), Decimal("15.00"))
        self.assertEqual(Decimal(cfg["business_dev_share_percent"]), Decimal("5.00"))

    # --- a direct project ---
    def test_a_direct_project_splits_70_15_15(self):
        split = self.project().payout_split()
        self.assertEqual(split["expert_percent"], Decimal("70.00"))
        self.assertEqual(split["expert_usd"], Decimal("7000.00"))
        self.assertEqual(split["delivery_lead_percent"], Decimal("15.00"))
        self.assertEqual(split["delivery_lead_usd"], Decimal("1500.00"))
        self.assertEqual(split["business_dev_usd"], Decimal("0.00"))
        self.assertEqual(split["platform_percent"], Decimal("15.00"))
        self.assertEqual(split["platform_usd"], Decimal("1500.00"))

    # --- a sourced project ---
    def test_a_sourced_project_splits_70_15_5_10(self):
        """The commission comes out of the platform's share, not the expert's."""
        split = self.project(with_bd=True).payout_split()
        self.assertEqual(split["expert_usd"], Decimal("7000.00"))
        self.assertEqual(split["delivery_lead_usd"], Decimal("1500.00"))
        self.assertEqual(split["business_dev_percent"], Decimal("5.00"))
        self.assertEqual(split["business_dev_usd"], Decimal("500.00"))
        self.assertEqual(split["platform_percent"], Decimal("10.00"))
        self.assertEqual(split["platform_usd"], Decimal("1000.00"))

    def test_a_business_developer_never_costs_the_delivery_side_anything(self):
        direct = self.project().payout_split()
        sourced = self.project(with_bd=True).payout_split()
        self.assertEqual(direct["expert_usd"], sourced["expert_usd"])
        self.assertEqual(direct["delivery_lead_usd"], sourced["delivery_lead_usd"])
        self.assertEqual(
            direct["platform_usd"] - sourced["platform_usd"],
            sourced["business_dev_usd"],
            "the commission should come entirely out of the platform's remainder",
        )

    # --- the invariant ---
    def test_the_split_always_closes_exactly(self):
        for quote in (1, 7, 99, 333, 1000, 3333, 10000, 99999):
            for with_bd in (False, True):
                with self.subTest(quote=quote, with_bd=with_bd):
                    split = self.project(with_bd=with_bd, quote=quote).payout_split()
                    total = (split["expert_usd"] + split["delivery_lead_usd"]
                             + split["business_dev_usd"] + split["platform_usd"])
                    self.assertEqual(total, Decimal(quote),
                                     "the four shares must sum to the quote exactly")

    def test_the_percentages_close_too(self):
        for with_bd in (False, True):
            with self.subTest(with_bd=with_bd):
                split = self.project(with_bd=with_bd).payout_split()
                total = (split["expert_percent"] + split["delivery_lead_percent"]
                         + split["business_dev_percent"] + split["platform_percent"])
                self.assertEqual(total, Decimal("100.00"))

    # --- the pool a lead actually hands out ---
    def test_the_expert_pool_follows_the_share(self):
        self.assertEqual(self.project().expert_pool_usd, Decimal("7000.00"))

    def test_a_sourced_project_has_the_same_pool(self):
        """The pool must not shrink because somebody sourced the work."""
        self.assertEqual(
            self.project(with_bd=True).expert_pool_usd, Decimal("7000.00"))

    # --- history is not restated ---
    def test_a_settled_project_keeps_the_split_it_was_paid_under(self):
        """The reason raising the share is safe: earnings snapshot their
        percentage at approval, so past work is never re-derived."""
        from payments.models import Earning

        project = self.project()
        project.stage = Project.Stage.COMPLETED
        project.save(update_fields=["stage"])
        Earning.objects.create(
            user=self.expert, project=project, kind=Earning.Kind.EXPERT,
            share_percent=Decimal("60.00"), amount_usd=Decimal("6000.00"))

        split = project.payout_split()
        self.assertEqual(split["expert_usd"], Decimal("6000.00"),
                         "reports the snapshot, not today's percentage")
        self.assertEqual(split["expert_percent"], Decimal("60.00"))

    # --- an override still wins ---
    def test_a_per_project_override_beats_the_default(self):
        project = self.project()
        project.expert_share_percent = Decimal("80.00")
        project.save(update_fields=["expert_share_percent"])
        split = project.payout_split()
        self.assertEqual(split["expert_usd"], Decimal("8000.00"))
        self.assertEqual(split["platform_percent"], Decimal("5.00"))

    def test_an_over_allocated_configuration_is_still_refused(self):
        """70 + 15 + 5 leaves 10 points of headroom; past 100 must still fail."""
        from django.core.exceptions import ValidationError

        project = self.project()
        project.expert_share_percent = Decimal("90.00")
        project.delivery_lead_share_percent = Decimal("15.00")
        with self.assertRaises(ValidationError):
            project.clean()


class ShareMigrationTests(TestCase):
    """The data migration that moved live installs from 60% to 70%.

    Raising `DEFAULT_EXPERT_SHARE` alone would have changed nothing on a running
    deployment — the default applies to rows that don't exist yet, and every
    install already has its one `SiteSettings` row saved with the old number.
    """

    def _migration(self):
        # Imported by path because the module name starts with a digit, so it
        # can't be reached with a normal `from ... import`.
        import importlib

        return importlib.import_module(
            "accounts.migrations.0018_expert_share_to_seventy")

    def _apps(self):
        """The live registry. The migration only calls `get_model`, so the real
        one behaves identically to the historical one Django would hand it."""
        from django.apps import apps

        return apps

    def test_a_row_still_on_the_old_default_is_raised(self):
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("60.00")
        row.save(update_fields=["expert_share_percent"])

        module = self._migration()
        module.raise_share(self._apps(), None)

        row.refresh_from_db()
        self.assertEqual(row.expert_share_percent, Decimal("70.00"))

    def test_a_deliberately_configured_value_is_left_alone(self):
        """A data migration that silently overwrites configured values is how a
        deploy quietly changes what people get paid."""
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("65.00")
        row.save(update_fields=["expert_share_percent"])

        module = self._migration()
        module.raise_share(self._apps(), None)

        row.refresh_from_db()
        self.assertEqual(row.expert_share_percent, Decimal("65.00"))

    def test_it_reverses(self):
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("70.00")
        row.save(update_fields=["expert_share_percent"])

        module = self._migration()
        module.lower_share(self._apps(), None)

        row.refresh_from_db()
        self.assertEqual(row.expert_share_percent, Decimal("60.00"))
