"""Who gets emailed, and who doesn't.

Notifications were addressed to `_lead_emails()` — every delivery lead on the
platform, approved or not, in any discipline. A payout request, a progress
update, a completed project: all of it landed in the inbox of people with no
stake in it. And because a multi-recipient send used one To: header, every
recipient could read every other recipient's address.

Both are fixed here. The rule is the same one project access already follows:
you hear about a project if you're on it, and about an unclaimed brief if you
could actually pick it up.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import ProductLine
from payments import notifications as payout_notifications
from payments.models import Withdrawal
from projects import notifications
from projects.models import Activity, Project

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def recipients():
    """Every address that received something, across all messages."""
    return {addr for m in mail.outbox for addr in m.to}


class NotificationScopeTests(TestCase):
    def setUp(self):
        self.design = ProductLine.objects.get(slug="design-creative")
        self.web = ProductLine.objects.get(slug="software-web")

        self.lead = User.objects.create_user(
            "owner@ril.team", "x", full_name="Owning Lead",
            role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.design)
        # Same discipline, different brief.
        self.peer = User.objects.create_user(
            "peer@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.peer.product_lines.add(self.design)
        # Another discipline entirely.
        self.foreign = User.objects.create_user(
            "foreign@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.foreign.product_lines.add(self.web)
        # Right discipline, still waiting to be approved.
        self.pending = User.objects.create_user(
            "pending@ril.team", "x", role=User.Role.DELIVERY_LEAD,
            approval_status=User.ApprovalStatus.PENDING)
        self.pending.product_lines.add(self.design)
        self.admin = User.objects.create_superuser("admin@ril.team", "x")

        self.expert = User.objects.create_user(
            "expert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.expert.product_lines.add(self.design)
        self.bystander = User.objects.create_user(
            "bystander@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "client@acme.io", "x", role=User.Role.CLIENT)

        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.design, lead=self.lead,
            expert=self.expert, stage=Project.Stage.IN_PROGRESS, quote_usd=2000)
        self.project.experts.add(self.expert)
        mail.outbox = []

    # --- the reported bug ---
    def test_a_payout_request_does_not_go_to_every_lead(self):
        """The example: "I made a payout request and it sent it to everyone"."""
        withdrawal = Withdrawal.objects.create(
            user=self.expert, reference="WD-1", amount_usd=Decimal("500.00"),
            bank_name="GTB", bank_account_number="0123456789",
            bank_account_name="An Expert")
        payout_notifications.notify_withdrawal_requested(withdrawal)

        got = recipients()
        self.assertIn(self.expert.email, got, "the requester gets their receipt")
        self.assertIn(self.admin.email, got, "somebody has to settle it")
        self.assertIn(self.lead.email, got, "their own lead")
        for outsider in (self.peer, self.foreign, self.pending):
            self.assertNotIn(outsider.email, got, outsider.email)

    def test_what_someone_earns_is_not_broadcast(self):
        """A payout request names an amount. That isn't general information."""
        withdrawal = Withdrawal.objects.create(
            user=self.expert, reference="WD-2", amount_usd=Decimal("500.00"),
            bank_name="GTB", bank_account_number="0123456789",
            bank_account_name="An Expert")
        payout_notifications.notify_withdrawal_requested(withdrawal)
        told = recipients()
        self.assertEqual(told, {self.expert.email, self.admin.email, self.lead.email})

    # --- project traffic ---
    def test_an_update_reaches_the_project_not_the_platform(self):
        activity = Activity.objects.create(
            project=self.project, author=self.expert, author_name="An Expert",
            role_label="Expert", kind=Activity.Kind.PROGRESS, text="Getting on")
        notifications.notify_update_posted(self.project, activity)

        got = recipients()
        self.assertEqual(got, {self.customer.email, self.lead.email},
                         "client and the lead running it — the author is excluded")
        self.assertNotIn(self.peer.email, got)
        self.assertNotIn(self.bystander.email, got)

    def test_completion_reaches_the_people_who_delivered_it(self):
        notifications.notify_project_completed(self.project)
        got = recipients()
        self.assertIn(self.lead.email, got)
        self.assertIn(self.expert.email, got)
        self.assertNotIn(self.peer.email, got)
        self.assertNotIn(self.foreign.email, got)

    # --- the intake queue ---
    def test_a_new_brief_reaches_the_leads_who_could_quote_it(self):
        brief = Project.objects.create(
            title="Unclaimed", client=self.customer, category="Brand identity",
            description="…", product_line=self.design, stage=Project.Stage.SUBMITTED)
        mail.outbox = []
        notifications.notify_project_submitted(brief)

        got = recipients()
        self.assertEqual(got, {self.lead.email, self.peer.email},
                         "both approved leads who run this discipline")

    def test_a_lead_in_another_discipline_is_not_told(self):
        brief = Project.objects.create(
            title="Unclaimed", client=self.customer, category="Brand identity",
            description="…", product_line=self.design, stage=Project.Stage.SUBMITTED)
        mail.outbox = []
        notifications.notify_project_submitted(brief)
        self.assertNotIn(self.foreign.email, recipients())

    def test_a_lead_still_in_review_is_not_told(self):
        """They can't quote it, so telling them is noise about work they can't take."""
        brief = Project.objects.create(
            title="Unclaimed", client=self.customer, category="Brand identity",
            description="…", product_line=self.design, stage=Project.Stage.SUBMITTED)
        mail.outbox = []
        notifications.notify_project_submitted(brief)
        self.assertNotIn(self.pending.email, recipients())

    def test_once_claimed_only_its_own_lead_hears_about_payment(self):
        notifications.notify_payment_received(self.project)
        got = recipients()
        self.assertIn(self.customer.email, got)
        self.assertIn(self.lead.email, got)
        self.assertNotIn(self.peer.email, got)


class AddressPrivacyTests(TestCase):
    """Nobody learns anybody else's email address from a notification."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "privlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.one = User.objects.create_user(
            "one@ril.dev", "x", role=User.Role.EXPERT)
        self.two = User.objects.create_user(
            "two@ril.dev", "x", role=User.Role.EXPERT)
        self.customer = User.objects.create_user(
            "privclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.one, stage=Project.Stage.IN_PROGRESS, quote_usd=2000)
        self.project.experts.add(self.one, self.two)
        mail.outbox = []

    def test_every_message_is_addressed_to_exactly_one_person(self):
        """A shared To: header showed each client, expert and lead the others'
        addresses — a disclosure that can't be taken back once sent."""
        notifications.notify_project_completed(self.project)
        self.assertGreater(len(mail.outbox), 1, "several people are told")
        for message in mail.outbox:
            self.assertEqual(len(message.to), 1, f"shared To: {message.to}")

    def test_everyone_still_gets_their_copy(self):
        """Splitting the send must not drop anyone."""
        notifications.notify_project_completed(self.project)
        self.assertEqual(
            recipients(), {self.lead.email, self.one.email, self.two.email})

    def test_one_bad_address_does_not_stop_the_rest(self):
        from unittest.mock import patch

        real_send = mail.EmailMultiAlternatives.send
        calls = {"n": 0}

        def flaky(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("mailbox full")
            return real_send(self, *a, **kw)

        with patch.object(mail.EmailMultiAlternatives, "send", flaky):
            notifications.notify_project_completed(self.project)
        self.assertGreaterEqual(calls["n"], 3, "kept going after the failure")
