"""Tests for the payout split — the arithmetic that decides who gets paid what.

This is the highest-consequence logic in the codebase: it divides real money, and
a project's split has to close on the quote exactly, rounding included. It is
also about to gain a fourth share (the business developer commission), so it is
covered here first.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import SiteSettings
from payments import earnings as earnings_service
from payments.models import Earning
from projects.models import Project

User = get_user_model()


class PayoutSplitTests(TestCase):
    def setUp(self):
        self.lead = User.objects.create_user(
            "lead@ril.team", "x", full_name="Lead One", role=User.Role.DELIVERY_LEAD
        )
        self.expert = User.objects.create_user(
            "expert@ril.dev", "x", full_name="Expert One", role=User.Role.EXPERT
        )
        self.customer = User.objects.create_user(
            "client@acme.io", "x", full_name="Client One", role=User.Role.CLIENT
        )

    def project(self, quote=1000, **kwargs):
        return Project.objects.create(
            title="A brief", client=self.customer, category="Design & branding",
            description="…", quote_usd=quote, lead=self.lead, expert=self.expert,
            **kwargs,
        )

    def assert_closes(self, split):
        """The parts must reconstitute the quote exactly — no lost cent."""
        total = split["expert_usd"] + split["delivery_lead_usd"] + split["platform_usd"]
        self.assertEqual(total, split["quote_usd"])

    def test_site_defaults_split_60_15_25(self):
        split = self.project(quote=1000).payout_split()
        self.assertEqual(split["expert_usd"], Decimal("600.00"))
        self.assertEqual(split["delivery_lead_usd"], Decimal("150.00"))
        self.assertEqual(split["platform_usd"], Decimal("250.00"))
        self.assertEqual(split["platform_percent"], Decimal("25.00"))
        self.assert_closes(split)

    def test_per_project_override_moves_the_platform_remainder(self):
        split = self.project(
            quote=1000,
            expert_share_percent=Decimal("70"),
            delivery_lead_share_percent=Decimal("20"),
        ).payout_split()
        self.assertEqual(split["expert_usd"], Decimal("700.00"))
        self.assertEqual(split["delivery_lead_usd"], Decimal("200.00"))
        self.assertEqual(split["platform_usd"], Decimal("100.00"))
        self.assertTrue(split["uses_override"])
        self.assert_closes(split)

    def test_one_blank_override_falls_back_to_the_site_default(self):
        split = self.project(
            quote=1000, expert_share_percent=Decimal("50")
        ).payout_split()
        self.assertEqual(split["expert_usd"], Decimal("500.00"))
        self.assertEqual(split["delivery_lead_usd"], Decimal("150.00"))  # site default
        self.assertEqual(split["platform_usd"], Decimal("350.00"))
        self.assert_closes(split)

    def test_split_closes_when_percentages_do_not_divide_evenly(self):
        """A third of an odd quote can't be represented in cents — the platform
        absorbs the remainder rather than the total drifting off the quote."""
        self.assert_closes(self.project(
            quote=1001,
            expert_share_percent=Decimal("33.33"),
            delivery_lead_share_percent=Decimal("33.33"),
        ).payout_split())

    def test_shares_over_100_percent_are_rejected(self):
        project = self.project(
            quote=1000,
            expert_share_percent=Decimal("80"),
            delivery_lead_share_percent=Decimal("30"),
        )
        with self.assertRaises(ValidationError):
            project.full_clean()

    def test_completed_project_reports_the_ledger_not_the_current_config(self):
        """The point of snapshotting: re-pricing after approval must not restate
        what someone was already paid."""
        project = self.project(quote=1000, stage=Project.Stage.COMPLETED)
        Earning.objects.create(project=project, user=self.expert,
                               kind=Earning.Kind.EXPERT,
                               share_percent=Decimal("60"), amount_usd=Decimal("600.00"))
        Earning.objects.create(project=project, user=self.lead,
                               kind=Earning.Kind.DELIVERY_LEAD,
                               share_percent=Decimal("15"), amount_usd=Decimal("150.00"))

        # Someone edits the site default afterwards.
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("90")
        row.delivery_lead_share_percent = Decimal("5")
        row.save()

        split = project.payout_split()
        self.assertTrue(split["is_settled"])
        self.assertEqual(split["expert_usd"], Decimal("600.00"))   # not 900
        self.assertEqual(split["delivery_lead_usd"], Decimal("150.00"))
        self.assert_closes(split)


class SiteSettingsTests(TestCase):
    def test_platform_share_is_the_remainder(self):
        """Two remainders: a sourced project pays the commission, a direct one
        keeps it."""
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("55")
        row.delivery_lead_share_percent = Decimal("20")
        row.business_dev_share_percent = Decimal("5")
        row.save()
        self.assertEqual(row.platform_share_percent, Decimal("20"))
        self.assertEqual(row.platform_share_direct_percent, Decimal("25"))

    def test_shares_over_100_percent_are_rejected(self):
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("80")
        row.delivery_lead_share_percent = Decimal("30")
        with self.assertRaises(ValidationError):
            row.clean()

    def test_the_commission_counts_towards_the_100_percent_guard(self):
        """Without this the three could be saved summing over 100%, and the
        platform's remainder would silently clamp to zero."""
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("60")
        row.delivery_lead_share_percent = Decimal("38")
        row.business_dev_share_percent = Decimal("5")  # 103% total
        with self.assertRaises(ValidationError):
            row.clean()


class EarningsCreditTests(TestCase):
    """Crediting on approval must be idempotent — nobody is paid twice."""

    def setUp(self):
        self.lead = User.objects.create_user(
            "lead2@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.expert = User.objects.create_user(
            "expert2@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "client2@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A brief", client=self.customer, category="UI/UX design",
            description="…", quote_usd=2000, lead=self.lead, expert=self.expert,
            stage=Project.Stage.COMPLETED,
        )

    def test_recording_twice_credits_once(self):
        first = earnings_service.record_project_earnings(self.project)
        second = earnings_service.record_project_earnings(self.project)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(Earning.objects.filter(project=self.project).count(), 2)

    def test_unapproved_project_credits_nothing(self):
        self.project.stage = Project.Stage.REVIEW
        self.project.save()
        self.assertEqual(earnings_service.record_project_earnings(self.project), [])
        self.assertEqual(Earning.objects.filter(project=self.project).count(), 0)

    def test_a_lead_who_is_also_the_expert_earns_both_shares(self):
        self.project.expert = self.lead
        self.project.save()
        earnings_service.record_project_earnings(self.project)
        kinds = set(Earning.objects.filter(user=self.lead).values_list("kind", flat=True))
        self.assertEqual(kinds, {Earning.Kind.EXPERT, Earning.Kind.DELIVERY_LEAD})
        total = sum(e.amount_usd for e in Earning.objects.filter(user=self.lead))
        self.assertEqual(total, Decimal("1500.00"))  # 60% + 15% of 2000


class AttachmentTests(TestCase):
    """Deliverable and reference links.

    The rule worth protecting: a client supplies references, the delivery team
    supplies deliverables. If a client could post a "deliverable" the record
    would misstate who produced the work.
    """

    def setUp(self):
        from catalog.models import ProductLine
        from rest_framework.test import APIClient

        self.APIClient = APIClient
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "lead3@ril.team", "x", full_name="A Lead", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "expert3@ril.dev", "x", full_name="An Expert", role=User.Role.EXPERT)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "client3@acme.io", "x", full_name="A Client", role=User.Role.CLIENT)
        self.outsider = User.objects.create_user(
            "nosy@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A logo", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=2000)

    def as_user(self, user):
        client = self.APIClient()
        client.force_authenticate(user=user)
        return client

    def post_link(self, user, **payload):
        return self.as_user(user).post(
            f"/api/projects/{self.project.id}/attachments",
            {"url": "https://figma.com/file/abc", **payload}, format="json")

    def test_the_kind_is_detected_from_the_url(self):
        from projects.models import Attachment

        cases = {
            "https://www.figma.com/file/abc": Attachment.Kind.FIGMA,
            "https://docs.google.com/document/d/1": Attachment.Kind.DRIVE,
            "https://github.com/org/repo": Attachment.Kind.GITHUB,
            "https://www.loom.com/share/x": Attachment.Kind.LOOM,
            "https://example.com/thing.pdf": Attachment.Kind.LINK,
        }
        for url, expected in cases.items():
            self.assertEqual(Attachment.detect_kind(url), expected, url)

    def test_a_lookalike_host_is_not_mistaken_for_the_real_one(self):
        from projects.models import Attachment

        self.assertEqual(
            Attachment.detect_kind("https://figma.com.evil.example/file"),
            Attachment.Kind.LINK,
        )

    def test_an_expert_adds_a_deliverable_and_it_is_recorded(self):
        response = self.post_link(self.expert, label="Logo concepts")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["purpose"], "deliverable")
        self.assertEqual(response.data["kind"], "figma")
        # The handover is part of the record, not a silent edit.
        self.assertTrue(
            self.project.activity.filter(text__contains="Logo concepts").exists())

    def test_a_client_link_is_always_a_reference(self):
        """Even if they ask for 'deliverable' — they didn't produce the work."""
        response = self.post_link(self.customer, purpose="deliverable")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["purpose"], "reference")

    def test_someone_elses_project_is_closed(self):
        self.assertEqual(self.post_link(self.outsider).status_code, 404)

    def test_a_missing_label_falls_back_to_the_host(self):
        response = self.post_link(self.expert)
        self.assertEqual(response.data["label"], "figma.com")

    def test_links_ride_along_with_a_progress_update(self):
        response = self.as_user(self.expert).post(
            f"/api/projects/{self.project.id}/activity",
            {"text": "First concepts are ready.", "kind": "milestone",
             "attachments": [{"url": "https://figma.com/file/xyz", "label": "Concepts v1"}]},
            format="json")
        self.assertEqual(response.status_code, 200)
        entry = self.project.activity.filter(kind="milestone").first()
        self.assertEqual(entry.attachments.count(), 1)
        # And it shows up in the project's deliverables, not only in the feed.
        self.assertEqual(self.project.attachments.count(), 1)

    def test_a_client_can_attach_references_to_their_brief(self):
        from catalog.models import Service

        service = Service.objects.get(product_line=self.line, name="Brand identity")
        response = self.as_user(self.customer).post("/api/projects", {
            "title": "New identity", "product_line": "design-creative",
            "service": service.id, "description": "A full rebrand.",
            "references": [
                {"url": "https://drive.google.com/folder/1", "label": "Our old assets"},
                {"url": "https://figma.com/file/moodboard"},
            ],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        project = Project.objects.get(id=response.data["id"])
        self.assertEqual(project.attachments.count(), 2)
        self.assertTrue(all(a.purpose == "reference" for a in project.attachments.all()))

    def test_the_owner_can_remove_their_own_link(self):
        link_id = self.post_link(self.expert).data["id"]
        response = self.as_user(self.expert).delete(f"/api/attachments/{link_id}")
        self.assertEqual(response.status_code, 204)

    def test_a_lead_can_remove_anyones_link(self):
        link_id = self.post_link(self.expert).data["id"]
        response = self.as_user(self.lead).delete(f"/api/attachments/{link_id}")
        self.assertEqual(response.status_code, 204)

    def test_nobody_else_can_remove_a_link(self):
        link_id = self.post_link(self.expert).data["id"]
        response = self.as_user(self.customer).delete(f"/api/attachments/{link_id}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.project.attachments.count(), 1)

    def test_a_malformed_url_is_rejected(self):
        response = self.post_link(self.expert, url="not-a-url")
        self.assertEqual(response.status_code, 400)


class ReportingTests(TestCase):
    """Reporting must agree with the ledger.

    The risk here isn't a crash, it's a number that looks authoritative and is
    wrong — a margin computed from current percentages rather than from what was
    actually paid would drift the moment an admin edits a share.
    """

    def setUp(self):
        from catalog.models import ProductLine
        from django.utils import timezone
        from rest_framework.test import APIClient

        self.APIClient = APIClient
        self.timezone = timezone
        self.software = ProductLine.objects.get(slug="software-web")
        self.design = ProductLine.objects.get(slug="design-creative")

        self.lead = User.objects.create_user(
            "rlead@ril.team", "x", full_name="R Lead", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.software, self.design)
        self.expert = User.objects.create_user(
            "rexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.software)
        self.bizdev = User.objects.create_user(
            "rbd@ril.team", "x", full_name="R Bizdev", role=User.Role.BUSINESS_DEV)
        self.customer = User.objects.create_user(
            "rclient@acme.io", "x", role=User.Role.CLIENT, referred_by=self.bizdev)
        self.admin = User.objects.create_superuser("radmin@ril.team", "x")

    def completed(self, line, quote, bizdev=None, days=10):
        project = Project.objects.create(
            title="A brief", client=self.customer, category="X", description="…",
            product_line=line, quote_usd=quote, lead=self.lead, expert=self.expert,
            business_developer=bizdev, stage=Project.Stage.COMPLETED,
            completed_at=self.timezone.now(),
        )
        Project.objects.filter(pk=project.pk).update(
            created_at=project.completed_at - self.timezone.timedelta(days=days))
        project.refresh_from_db()
        earnings_service.record_project_earnings(project)
        return project

    def report(self, user):
        client = self.APIClient()
        client.force_authenticate(user=user)
        return client.get("/api/reports")

    def test_the_platform_share_is_delivered_value_minus_the_ledger(self):
        self.completed(self.software, 10000)          # direct: platform keeps 25%
        self.completed(self.design, 10000, self.bizdev)  # sourced: platform keeps 20%

        totals = self.report(self.admin).data["totals"]
        self.assertEqual(totals["delivered_value_usd"], "20000.00")
        # 7500 + 8000 credited out across the two projects.
        self.assertEqual(totals["paid_out_usd"], "15500.00")
        self.assertEqual(totals["platform_usd"], "4500.00")
        self.assertEqual(totals["margin_percent"], "22.50")

    def test_a_lines_margin_reflects_whether_work_was_sourced(self):
        self.completed(self.software, 10000)
        self.completed(self.design, 10000, self.bizdev)

        rows = {r["slug"]: r for r in self.report(self.admin).data["product_lines"]}
        self.assertEqual(rows["software-web"]["margin_percent"], "25.00")
        self.assertEqual(rows["software-web"]["bizdev_cost_usd"], "0.00")
        self.assertEqual(rows["design-creative"]["margin_percent"], "20.00")
        self.assertEqual(rows["design-creative"]["bizdev_cost_usd"], "500.00")

    def test_reporting_ignores_a_later_change_to_the_percentages(self):
        """The ledger is the record — editing a share can't restate history."""
        self.completed(self.software, 10000)
        row = SiteSettings.load()
        row.expert_share_percent = Decimal("90")
        row.delivery_lead_share_percent = Decimal("5")
        row.save()

        totals = self.report(self.admin).data["totals"]
        self.assertEqual(totals["platform_usd"], "2500.00")  # not 500

    def test_cycle_time_reports_its_sample_size(self):
        self.completed(self.software, 1000, days=10)
        self.completed(self.software, 1000, days=20)
        # A project delivered before completion dates were recorded.
        Project.objects.create(
            title="Old", client=self.customer, category="X", description="…",
            product_line=self.software, quote_usd=1000, lead=self.lead,
            stage=Project.Stage.COMPLETED, completed_at=None)

        totals = self.report(self.admin).data["totals"]
        self.assertEqual(totals["avg_cycle_days"], 15.0)
        # The undated project is excluded, and the count says so.
        self.assertEqual(totals["cycle_sample"], 2)
        self.assertEqual(totals["delivered_count"], 3)

    def test_a_lead_only_reports_on_their_own_lines(self):
        from catalog.models import ProductLine

        research = ProductLine.objects.get(slug="data-research")
        other_lead = User.objects.create_user(
            "olead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        other_lead.product_lines.add(research)
        Project.objects.create(
            title="Theirs", client=self.customer, category="X", description="…",
            product_line=research, quote_usd=9999, lead=other_lead,
            stage=Project.Stage.COMPLETED, completed_at=self.timezone.now())
        self.completed(self.software, 1000)

        data = self.report(self.lead).data
        slugs = {r["slug"] for r in data["product_lines"]}
        self.assertIn("software-web", slugs)
        self.assertNotIn("data-research", slugs)
        self.assertEqual(data["totals"]["delivered_value_usd"], "1000.00")

    def test_the_people_leaderboards_are_admin_only(self):
        """A lead reporting on their own lines is fine; ranking their peers isn't."""
        lead_view = self.report(self.lead).data
        self.assertNotIn("business_developers", lead_view)
        self.assertNotIn("delivery_leads", lead_view)

        admin_view = self.report(self.admin).data
        self.assertIn("business_developers", admin_view)
        self.assertIn("delivery_leads", admin_view)

    def test_the_bizdev_leaderboard_counts_conversion(self):
        self.completed(self.software, 10000, self.bizdev)
        Project.objects.create(  # sourced but not yet delivered
            title="Open", client=self.customer, category="X", description="…",
            product_line=self.software, quote_usd=5000, lead=self.lead,
            business_developer=self.bizdev, stage=Project.Stage.IN_PROGRESS)

        row = self.report(self.admin).data["business_developers"][0]
        self.assertEqual(row["projects_sourced"], 2)
        self.assertEqual(row["projects_won"], 1)
        self.assertEqual(row["sourced_value_usd"], 15000)
        self.assertEqual(row["commission_earned_usd"], "500.00")
        self.assertEqual(row["conversion_percent"], "50.00")

    def test_the_lead_scorecard_counts_team_and_delivery(self):
        self.completed(self.software, 4000, days=8)
        Project.objects.create(
            title="Open", client=self.customer, category="X", description="…",
            product_line=self.software, quote_usd=2000, lead=self.lead,
            stage=Project.Stage.IN_PROGRESS)

        row = next(r for r in self.report(self.admin).data["delivery_leads"]
                   if r["id"] == self.lead.id)
        self.assertEqual(row["team_size"], 1)
        self.assertEqual(row["projects_led"], 2)
        self.assertEqual(row["projects_delivered"], 1)
        self.assertEqual(row["delivered_value_usd"], 4000)
        self.assertEqual(row["in_flight_value_usd"], 2000)
        self.assertEqual(row["earned_usd"], "600.00")
        self.assertEqual(row["avg_cycle_days"], 8.0)

    def test_an_empty_platform_reports_nulls_not_zeros(self):
        """A margin of 0% and 'no data yet' are different claims."""
        totals = self.report(self.admin).data["totals"]
        self.assertEqual(totals["delivered_count"], 0)
        self.assertIsNone(totals["margin_percent"])
        self.assertIsNone(totals["avg_cycle_days"])

    def test_an_expert_cannot_read_reports(self):
        self.assertEqual(self.report(self.expert).status_code, 403)


class OnTimeTests(TestCase):
    """Delivery punctuality.

    The rule that matters: "no target agreed" and "missed the target" are
    different states. A project nobody promised a date for must be excluded from
    the sample, never counted as a miss — otherwise every team's rate is
    understated by whatever they haven't scheduled yet.
    """

    def setUp(self):
        from catalog.models import ProductLine
        from django.utils import timezone
        from rest_framework.test import APIClient

        self.APIClient = APIClient
        self.timezone = timezone
        self.today = timezone.localdate()
        self.line = ProductLine.objects.get(slug="software-web")
        self.lead = User.objects.create_user(
            "otlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "otexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.line)
        self.customer = User.objects.create_user(
            "otclient@acme.io", "x", role=User.Role.CLIENT)
        self.admin = User.objects.create_superuser("otadmin@ril.team", "x")

    def make(self, target_offset=None, completed_offset=None, stage=None):
        """target_offset/completed_offset are days from today; None means unset."""
        project = Project.objects.create(
            title="A brief", client=self.customer, category="X", description="…",
            product_line=self.line, quote_usd=1000, lead=self.lead, expert=self.expert,
            stage=stage or (Project.Stage.COMPLETED if completed_offset is not None
                            else Project.Stage.IN_PROGRESS),
            target_date=(self.today + self.timezone.timedelta(days=target_offset)
                         if target_offset is not None else None),
            completed_at=(self.timezone.now() + self.timezone.timedelta(days=completed_offset)
                          if completed_offset is not None else None),
        )
        if project.stage == Project.Stage.COMPLETED:
            earnings_service.record_project_earnings(project)
        return project

    def report(self, user):
        client = self.APIClient()
        client.force_authenticate(user=user)
        return client.get("/api/reports").data

    def test_delivered_before_the_target_is_on_time(self):
        project = self.make(target_offset=5, completed_offset=0)
        self.assertIs(project.is_on_time, True)
        self.assertEqual(project.days_late, -5)

    def test_delivered_on_the_target_day_is_on_time(self):
        """The promise is 'by' the date, so landing on it counts."""
        project = self.make(target_offset=0, completed_offset=0)
        self.assertIs(project.is_on_time, True)
        self.assertEqual(project.days_late, 0)

    def test_delivered_after_the_target_is_late(self):
        project = self.make(target_offset=-3, completed_offset=0)
        self.assertIs(project.is_on_time, False)
        self.assertEqual(project.days_late, 3)

    def test_no_target_means_the_question_does_not_apply(self):
        project = self.make(target_offset=None, completed_offset=0)
        self.assertIsNone(project.is_on_time)
        self.assertIsNone(project.days_late)

    def test_an_undelivered_project_has_no_verdict_yet(self):
        project = self.make(target_offset=5)
        self.assertIsNone(project.is_on_time)

    def test_overdue_is_a_live_project_past_its_date(self):
        self.assertTrue(self.make(target_offset=-1).is_overdue)
        self.assertFalse(self.make(target_offset=1).is_overdue)

    def test_a_project_with_no_target_is_never_overdue(self):
        """An unagreed date is not a broken promise."""
        self.assertFalse(self.make(target_offset=None).is_overdue)

    def test_a_delivered_project_is_never_overdue_even_if_late(self):
        project = self.make(target_offset=-10, completed_offset=0)
        self.assertFalse(project.is_overdue)
        self.assertIs(project.is_on_time, False)

    def test_the_rate_excludes_undated_projects_rather_than_failing_them(self):
        self.make(target_offset=5, completed_offset=0)    # on time
        self.make(target_offset=-2, completed_offset=0)   # late
        self.make(target_offset=None, completed_offset=0)  # no target — excluded

        totals = self.report(self.admin)["totals"]
        self.assertEqual(totals["delivered_count"], 3)
        # 1 of 2 dated deliveries, not 1 of 3.
        self.assertEqual(totals["on_time_percent"], 50.0)
        self.assertEqual(totals["on_time_sample"], 2)

    def test_the_rate_is_null_when_nothing_dated_has_shipped(self):
        self.make(target_offset=None, completed_offset=0)
        totals = self.report(self.admin)["totals"]
        self.assertIsNone(totals["on_time_percent"])
        self.assertEqual(totals["on_time_sample"], 0)

    def test_overdue_work_is_counted_for_the_lead(self):
        self.make(target_offset=-4)   # overdue
        self.make(target_offset=-1)   # overdue
        self.make(target_offset=10)   # fine
        self.make(target_offset=None)  # no date

        data = self.report(self.admin)
        self.assertEqual(data["totals"]["overdue_count"], 2)
        row = next(r for r in data["delivery_leads"] if r["id"] == self.lead.id)
        self.assertEqual(row["overdue_count"], 2)

    def test_the_rate_appears_per_product_line(self):
        self.make(target_offset=5, completed_offset=0)
        self.make(target_offset=-2, completed_offset=0)
        row = next(r for r in self.report(self.admin)["product_lines"]
                   if r["slug"] == "software-web")
        self.assertEqual(row["on_time_percent"], 50.0)
        self.assertEqual(row["on_time_sample"], 2)

    def test_a_lead_can_set_and_clear_the_target_date(self):
        project = self.make(target_offset=5, stage=Project.Stage.QUOTED)
        client = self.APIClient()
        client.force_authenticate(user=self.lead)

        moved = self.today + self.timezone.timedelta(days=20)
        response = client.patch(f"/api/projects/{project.id}/edit",
                                {"target_date": moved.isoformat()}, format="json")
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.target_date, moved)
        # The move is on the record, in words rather than ISO.
        self.assertTrue(project.activity.filter(text__contains="target date").exists())

        response = client.patch(f"/api/projects/{project.id}/edit",
                                {"target_date": None}, format="json")
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertIsNone(project.target_date)

    def test_a_nonsense_date_is_rejected(self):
        project = self.make(target_offset=5, stage=Project.Stage.QUOTED)
        client = self.APIClient()
        client.force_authenticate(user=self.lead)
        response = client.patch(f"/api/projects/{project.id}/edit",
                                {"target_date": "sometime in August"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_delivered_but_uncredited_project_does_not_inflate_margin(self):
        """Crediting on approval is best-effort and heals lazily. Reporting must
        not read an uncredited project as pure platform margin — that is wrong,
        and wrong in the flattering direction."""
        project = Project.objects.create(
            title="Never credited", client=self.customer, category="X",
            description="…", product_line=self.line, quote_usd=10000,
            lead=self.lead, expert=self.expert,
            stage=Project.Stage.COMPLETED, completed_at=self.timezone.now(),
        )
        self.assertEqual(project.earnings.count(), 0)

        totals = self.report(self.admin)["totals"]
        # Not 100%: reporting credits what approval missed.
        self.assertEqual(totals["margin_percent"], "25.00")
        self.assertEqual(totals["paid_out_usd"], "7500.00")
        self.assertEqual(project.earnings.count(), 2)

    def test_days_overdue_counts_only_live_work(self):
        """The board's chip needs a number, and it must come from the same clock
        as the flag rather than being recomputed in the browser."""
        overdue = self.make(target_offset=-3)
        self.assertTrue(overdue.is_overdue)
        self.assertEqual(overdue.days_overdue, 3)

        on_track = self.make(target_offset=3)
        self.assertIsNone(on_track.days_overdue)

        undated = self.make(target_offset=None)
        self.assertIsNone(undated.days_overdue)

        # Late but delivered: the verdict is "late", not "overdue" — a finished
        # project can't still be running out of time.
        delivered_late = self.make(target_offset=-5, completed_offset=0)
        self.assertIsNone(delivered_late.days_overdue)
        self.assertIs(delivered_late.is_on_time, False)

    def test_the_board_serializes_the_overdue_flag_and_count(self):
        self.make(target_offset=-2)
        client = self.APIClient()
        client.force_authenticate(user=self.lead)
        row = next(p for p in client.get("/api/projects").data if p["is_overdue"])
        self.assertEqual(row["days_overdue"], 2)
