"""The notification bell (G7).

Sixteen notification types terminated in SMTP alone, which quietly made email
deliverability the product's uptime: one spam classification and a client
silently stops responding — which, since G3, is the exact condition under which
a delivery lead may close a project over their head.

The design decision worth testing is where the in-app row gets written. It
happens inside `send_brand_email`, not at the call sites, so the two channels
share one recipient list *by construction*. The parity tests below are what
make that claim checkable rather than merely intended.
"""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Notification
from catalog.models import ProductLine
from projects.models import Project
from ripple.mailer import send_brand_email

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class ParityTests(TestCase):
    """Whatever is emailed reaches the bell, and nothing else does."""

    def setUp(self):
        self.a = User.objects.create_user("na@ril.dev", "x", full_name="A")
        self.b = User.objects.create_user("nb@ril.dev", "x", full_name="B")

    def test_every_recipient_of_an_email_gets_a_notification(self):
        send_brand_email(
            subject="Something happened",
            to=[self.a.email, self.b.email],
            heading="Heads up",
            paragraphs=["The first line becomes the body."],
        )
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(
            set(Notification.objects.values_list("user__email", flat=True)),
            {self.a.email, self.b.email},
        )

    def test_the_body_and_link_come_from_the_same_content(self):
        send_brand_email(
            subject="A project changed",
            to=self.a.email,
            heading="Heads up",
            paragraphs=["The client asked for changes."],
            cta=("Open the project", "http://localhost:5180/projects/7"),
        )
        row = Notification.objects.get()
        self.assertEqual(row.title, "A project changed")
        self.assertEqual(row.body, "The client asked for changes.")
        self.assertEqual(row.url, "/projects/7",
                         "stored as a path, so it survives a domain change")

    def test_an_address_with_no_account_is_skipped(self):
        send_brand_email(
            subject="Invitation", to="nobody@example.com",
            heading="Join us", paragraphs=["Hello."])
        self.assertEqual(Notification.objects.count(), 0)

    def test_notify_false_sends_mail_and_no_notification(self):
        mail.outbox = []
        send_brand_email(
            subject="Reset your password", to=self.a.email,
            heading="Reset", paragraphs=["Click here."], notify=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(Notification.objects.count(), 0)

    def test_pre_login_mail_never_reaches_a_bell(self):
        """Verification and reset go to people who aren't signed in; an
        invitation goes to somebody with no account at all."""
        from accounts.emails import send_password_reset_email, send_verification_email

        send_verification_email(self.a)
        send_password_reset_email(self.a)
        self.assertEqual(Notification.objects.count(), 0)

    def test_an_inactive_user_is_skipped(self):
        self.b.is_active = False
        self.b.save(update_fields=["is_active"])
        send_brand_email(
            subject="Hello", to=[self.a.email, self.b.email],
            heading="Hi", paragraphs=["There."])
        self.assertEqual(Notification.objects.count(), 1)

    def test_a_bell_failure_never_stops_the_email(self):
        """The email is the channel that has always worked; the bell must not
        be able to take it down."""
        from unittest.mock import patch

        mail.outbox = []
        with patch("accounts.models.Notification.objects.bulk_create",
                   side_effect=RuntimeError("db is on fire")):
            send_brand_email(
                subject="Still sends", to=self.a.email,
                heading="Hi", paragraphs=["There."])
        self.assertEqual(len(mail.outbox), 1)


class LifecycleParityTests(TestCase):
    """A real lifecycle action, checked end to end rather than through the helper."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "nlead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.expert = User.objects.create_user(
            "nexpert@ril.dev", "x", role=User.Role.EXPERT, lead=self.lead)
        self.customer = User.objects.create_user(
            "nclient@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A rebrand", client=self.customer, category="Brand identity",
            description="…", product_line=self.line, lead=self.lead,
            expert=self.expert, stage=Project.Stage.REVIEW, quote_usd=1000)
        self.project.experts.add(self.expert)

    def test_requesting_changes_rings_the_teams_bells(self):
        Notification.objects.all().delete()
        mail.outbox = []
        as_user(self.customer).post(
            f"/api/projects/{self.project.id}/request-changes",
            {"note": "The colours are wrong."}, format="json")

        emailed = {addr for m in mail.outbox for addr in m.to}
        belled = set(Notification.objects.values_list("user__email", flat=True))
        self.assertEqual(emailed, belled,
                         "the two channels reached different people")
        self.assertIn(self.lead.email, belled)
        self.assertIn(self.expert.email, belled)


class BellApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("bell@ril.dev", "x")
        self.other = User.objects.create_user("bellother@ril.dev", "x")
        for n in range(3):
            Notification.objects.create(
                user=self.user, title=f"Thing {n}", body="…", url="/projects/1")
        Notification.objects.create(user=self.other, title="Not yours", body="…")

    def test_you_only_see_your_own(self):
        response = as_user(self.user).get("/api/notifications")
        self.assertEqual(response.status_code, 200)
        titles = {n["title"] for n in response.data["notifications"]}
        self.assertEqual(titles, {"Thing 0", "Thing 1", "Thing 2"})
        self.assertEqual(response.data["unread"], 3)

    def test_marking_everything_read(self):
        response = as_user(self.user).post("/api/notifications", {}, format="json")
        self.assertEqual(response.data["unread"], 0)
        self.assertEqual(
            Notification.objects.filter(user=self.user, read_at__isnull=True).count(), 0)

    def test_marking_one_read(self):
        target = Notification.objects.filter(user=self.user).first()
        response = as_user(self.user).post(
            "/api/notifications", {"ids": [target.id]}, format="json")
        self.assertEqual(response.data["unread"], 2)

    def test_you_cannot_mark_somebody_elses_read(self):
        theirs = Notification.objects.get(user=self.other)
        as_user(self.user).post(
            "/api/notifications", {"ids": [theirs.id]}, format="json")
        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_the_unread_count_rides_on_auth_me(self):
        """No second request per page load just to draw a badge."""
        response = as_user(self.user).get("/api/auth/me")
        self.assertEqual(response.data["unread_notifications"], 3)

    def test_newest_first(self):
        latest = Notification.objects.create(
            user=self.user, title="Newest", body="…")
        response = as_user(self.user).get("/api/notifications")
        self.assertEqual(response.data["notifications"][0]["title"], "Newest")
