"""Stripe as a second collection rail (G8).

Quotes are stored in USD and always were. What changes is which provider bills
the client's card, decided by the buyer's currency rather than by anybody
choosing — NGN can only go one way, GBP can only go the other, and USD is the
overlap the platform's own setting breaks.

Payouts are untouched and stay on Paystack. Nothing here goes near a
withdrawal, and a test says so.

The webhook signature check gets the most attention, because it is the only
thing between a public URL and anyone marking their own project paid.
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Organisation, OrganisationMember
from catalog.models import ProductLine
from payments import gateways, stripe_gateway
from payments.models import Payment, Refund
from projects.models import Project

User = get_user_model()

LIVE_KEYS = dict(
    STRIPE_SECRET_KEY="sk_test_stripe",
    STRIPE_PUBLIC_KEY="pk_test_stripe",
    STRIPE_WEBHOOK_SECRET="whsec_test",
    PAYSTACK_SECRET_KEY="sk_test_paystack",
)


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def sign(payload, secret="whsec_test", timestamp=None):
    """Build the header Stripe would send, so the real check can be exercised."""
    timestamp = timestamp or int(time.time())
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


class RoutingTests(TestCase):
    """Which rail, and why."""

    @override_settings(PAYSTACK_CURRENCY="NGN", **LIVE_KEYS)
    def test_naira_can_only_go_through_paystack(self):
        self.assertEqual(gateways.choose("NGN"), gateways.PAYSTACK)

    @override_settings(PAYSTACK_CURRENCY="NGN", **LIVE_KEYS)
    def test_sterling_and_euros_can_only_go_through_stripe(self):
        for currency in ("GBP", "EUR", "CAD", "AUD"):
            with self.subTest(currency=currency):
                self.assertEqual(gateways.choose(currency), gateways.STRIPE)

    @override_settings(PAYSTACK_CURRENCY="NGN", **LIVE_KEYS)
    def test_dollars_follow_the_platform_default(self):
        """The overlap. A platform collecting in naira sends its dollar work to
        Stripe; one already collecting in dollars keeps it on Paystack."""
        self.assertEqual(gateways.choose("USD"), gateways.STRIPE)
        with override_settings(PAYSTACK_CURRENCY="USD"):
            self.assertEqual(gateways.choose("USD"), gateways.PAYSTACK)

    @override_settings(PAYSTACK_CURRENCY="NGN", STRIPE_SECRET_KEY="",
                       PAYSTACK_SECRET_KEY="sk_test_paystack")
    def test_without_stripe_keys_nothing_changes(self):
        """The platform behaves exactly as it did before Stripe existed."""
        self.assertEqual(gateways.choose("USD"), gateways.PAYSTACK)
        self.assertEqual(gateways.choose("NGN"), gateways.PAYSTACK)
        with self.assertRaises(gateways.UnsupportedCurrency):
            gateways.choose("GBP")

    @override_settings(PAYSTACK_CURRENCY="NGN", **LIVE_KEYS)
    def test_a_currency_nobody_supports_is_refused_clearly(self):
        with self.assertRaises(gateways.UnsupportedCurrency) as caught:
            gateways.choose("JPY")
        self.assertIn("JPY", str(caught.exception))


class CurrencyPreferenceTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.org = Organisation.objects.create(
            name="Nordic AB", slug="nordic-ab", preferred_currency="EUR")
        self.buyer = User.objects.create_user(
            "eur@nordic.io", "x", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=self.org, user=self.buyer, role="owner")
        self.project = Project.objects.create(
            title="A brief", client=self.buyer, organisation=self.org,
            category="Brand identity", description="…", product_line=self.line,
            stage=Project.Stage.QUOTED, quote_usd=5000)

    @override_settings(PAYSTACK_CURRENCY="NGN")
    def test_the_company_decides_the_currency(self):
        self.assertEqual(gateways.currency_for(self.project), "EUR")

    @override_settings(PAYSTACK_CURRENCY="NGN")
    def test_no_preference_falls_back_to_the_platform(self):
        self.org.preferred_currency = ""
        self.org.save(update_fields=["preferred_currency"])
        self.assertEqual(gateways.currency_for(self.project), "NGN")

    @override_settings(PAYSTACK_CURRENCY="NGN")
    def test_a_project_with_no_company_still_works(self):
        orphan = Project.objects.create(
            title="Legacy", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line,
            stage=Project.Stage.QUOTED, quote_usd=100)
        self.assertEqual(gateways.currency_for(orphan), "NGN")

    @override_settings(PAYSTACK_CURRENCY="NGN", **LIVE_KEYS)
    def test_a_euro_buyer_is_sent_to_stripe(self):
        with patch("payments.stripe_gateway._post") as post:
            post.return_value = {
                "id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
            result = as_user(self.buyer).post(
                f"/api/projects/{self.project.id}/pay/initialize").data

        self.assertEqual(result["gateway"], "stripe")
        self.assertEqual(result["currency"], "EUR")
        self.assertTrue(result["authorization_url"].startswith("https://checkout.stripe.com"))
        payment = Payment.objects.get()
        self.assertEqual(payment.gateway, "stripe")
        self.assertEqual(payment.currency, "EUR")
        # The processing fee rides along exactly as on the other rail.
        self.assertEqual(payment.usd_total, Decimal("5075.00"))


class WebhookSignatureTests(TestCase):
    """The only thing between a public URL and free projects."""

    def payload(self, reference="RIL-ABC"):
        return json.dumps({
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {"object": {
                "client_reference_id": reference, "payment_status": "paid"}},
        }).encode()

    @override_settings(**LIVE_KEYS)
    def test_a_correctly_signed_event_is_accepted(self):
        body = self.payload()
        event = stripe_gateway.verify_webhook(body, sign(body))
        self.assertEqual(event["type"], "checkout.session.completed")

    @override_settings(**LIVE_KEYS)
    def test_a_forged_signature_is_refused(self):
        body = self.payload()
        with self.assertRaises(stripe_gateway.StripeError):
            stripe_gateway.verify_webhook(body, "t=1,v1=deadbeef")

    @override_settings(**LIVE_KEYS)
    def test_a_signature_from_the_wrong_secret_is_refused(self):
        body = self.payload()
        with self.assertRaises(stripe_gateway.StripeError):
            stripe_gateway.verify_webhook(body, sign(body, secret="whsec_someone_else"))

    @override_settings(**LIVE_KEYS)
    def test_a_tampered_body_is_refused(self):
        header = sign(self.payload())
        with self.assertRaises(stripe_gateway.StripeError):
            stripe_gateway.verify_webhook(self.payload("RIL-SOMEONE-ELSE"), header)

    @override_settings(**LIVE_KEYS)
    def test_a_replayed_event_is_refused(self):
        """A captured request must not stay valid forever."""
        body = self.payload()
        old = int(time.time()) - (stripe_gateway.SIGNATURE_TOLERANCE_SECONDS + 60)
        with self.assertRaises(stripe_gateway.StripeError):
            stripe_gateway.verify_webhook(body, sign(body, timestamp=old))

    @override_settings(**LIVE_KEYS)
    def test_a_rotating_secret_does_not_drop_events(self):
        """Stripe sends several v1 signatures mid-rotation; any match counts."""
        body = self.payload()
        ts = int(time.time())
        good = sign(body, timestamp=ts).split("v1=")[1]
        header = f"t={ts},v1=deadbeef,v1={good}"
        self.assertTrue(stripe_gateway.verify_webhook(body, header))

    @override_settings(**LIVE_KEYS)
    def test_malformed_headers_are_refused(self):
        body = self.payload()
        for header in ("", "nonsense", "t=abc,v1=x", "v1=x"):
            with self.subTest(header=header):
                with self.assertRaises(stripe_gateway.StripeError):
                    stripe_gateway.verify_webhook(body, header)

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_an_unconfigured_secret_refuses_everything(self):
        body = self.payload()
        with self.assertRaises(stripe_gateway.StripeError):
            stripe_gateway.verify_webhook(body, sign(body))


class WebhookEndpointTests(TestCase):
    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.buyer = User.objects.create_user(
            "wh@acme.io", "x", role=User.Role.CLIENT)
        self.project = Project.objects.create(
            title="A brief", client=self.buyer, category="Brand identity",
            description="…", product_line=self.line,
            stage=Project.Stage.QUOTED, quote_usd=1000)
        self.payment = Payment.objects.create(
            project=self.project, gateway="stripe", reference="RIL-WH1",
            access_code="cs_test_1", amount_subunit=101500, currency="USD",
            usd_total=Decimal("1015.00"), status=Payment.Status.PENDING)

    def body(self, **overrides):
        obj = {"client_reference_id": "RIL-WH1", "payment_status": "paid"}
        obj.update(overrides.pop("object", {}))
        return json.dumps({
            "id": "evt_1",
            "type": overrides.get("type", "checkout.session.completed"),
            "data": {"object": obj},
        }).encode()

    @override_settings(**LIVE_KEYS)
    def test_a_paid_session_advances_the_project(self):
        body = self.body()
        response = self.client.post(
            "/api/stripe/webhook", data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=sign(body))
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db(); self.project.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCESS)
        self.assertEqual(self.project.stage, Project.Stage.PAID)

    @override_settings(**LIVE_KEYS)
    def test_an_unsigned_request_changes_nothing(self):
        response = self.client.post(
            "/api/stripe/webhook", data=self.body(),
            content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.QUOTED)

    @override_settings(**LIVE_KEYS)
    def test_an_unpaid_session_is_ignored(self):
        """A bank debit that hasn't cleared must not start work."""
        body = self.body(object={"payment_status": "unpaid"})
        self.client.post(
            "/api/stripe/webhook", data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=sign(body))
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.QUOTED)

    @override_settings(**LIVE_KEYS)
    def test_a_delayed_payment_succeeding_later_still_counts(self):
        body = self.body(type="checkout.session.async_payment_succeeded")
        self.client.post(
            "/api/stripe/webhook", data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=sign(body))
        self.project.refresh_from_db()
        self.assertEqual(self.project.stage, Project.Stage.PAID)

    @override_settings(**LIVE_KEYS)
    def test_delivering_the_same_event_twice_is_harmless(self):
        body = self.body()
        for _ in range(2):
            self.client.post(
                "/api/stripe/webhook", data=body, content_type="application/json",
                HTTP_STRIPE_SIGNATURE=sign(body))
        self.assertEqual(
            self.project.activity.filter(text__icontains="Paid the invoice").count(), 1)


class RailAgnosticTests(TestCase):
    """A project behaves the same downstream whichever rail paid for it."""

    def setUp(self):
        self.line = ProductLine.objects.get(slug="design-creative")
        self.lead = User.objects.create_user(
            "ralead@ril.team", "x", role=User.Role.DELIVERY_LEAD)
        self.lead.product_lines.add(self.line)
        self.buyer = User.objects.create_user(
            "ra@acme.io", "x", role=User.Role.CLIENT)
        self.admin = User.objects.create_superuser("raboss@ril.team", "x")

    def paid_project(self, gateway):
        project = Project.objects.create(
            title=f"Paid via {gateway}", client=self.buyer,
            category="Brand identity", description="…", product_line=self.line,
            lead=self.lead, stage=Project.Stage.IN_PROGRESS, quote_usd=2000)
        Payment.objects.create(
            project=project, gateway=gateway, reference=f"RIL-{gateway}",
            access_code="cs_x", amount_subunit=200000, currency="USD",
            usd_total=Decimal("2000.00"), status=Payment.Status.SUCCESS,
            paid_at=timezone.now())
        return project

    def test_collected_totals_read_the_same(self):
        for gateway in ("paystack", "stripe"):
            with self.subTest(gateway=gateway):
                project = self.paid_project(gateway)
                self.assertEqual(project.collected_usd, Decimal("2000.00"))
                self.assertEqual(project.refundable_usd, Decimal("2000.00"))

    @override_settings(PAYSTACK_REFUNDS_ENABLED=False, STRIPE_SECRET_KEY="")
    def test_a_refund_is_recorded_on_either_rail_when_gateways_are_off(self):
        for gateway in ("paystack", "stripe"):
            with self.subTest(gateway=gateway):
                project = self.paid_project(gateway)
                response = as_user(self.admin).post(
                    f"/api/projects/{project.id}/refunds",
                    {"amount_usd": "100", "reason": "Goodwill."}, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                refund = Refund.objects.filter(project=project).get()
                self.assertEqual(refund.status, Refund.Status.PROCESSED)
                self.assertTrue(refund.settled_manually)

    @override_settings(**LIVE_KEYS)
    def test_a_refund_goes_back_down_the_rail_it_came_up(self):
        """Never re-routed to today's default — the money can only return to
        the provider that received it."""
        project = self.paid_project("stripe")
        with patch("payments.stripe_gateway._get") as get, \
             patch("payments.stripe_gateway._post") as post:
            get.return_value = {"payment_intent": "pi_test_1"}
            post.return_value = {"id": "re_test_1"}
            as_user(self.admin).post(
                f"/api/projects/{project.id}/refunds",
                {"amount_usd": "100", "reason": "Goodwill."}, format="json")

        refund = Refund.objects.get(project=project)
        self.assertEqual(refund.gateway, "stripe")
        self.assertEqual(refund.gateway_reference, "re_test_1")
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "/refunds")

    def test_payouts_are_untouched_by_any_of_this(self):
        """Stripe is a collection rail. Payouts stay on Paystack."""
        import inspect

        from payments import transfers

        source = inspect.getsource(transfers)
        self.assertNotIn("stripe", source.lower())


class CurrencySettingTests(TestCase):
    """Setting the currency decides which rail can bill you, so a typo here
    would leave a company unable to pay at all."""

    def setUp(self):
        self.org = Organisation.objects.create(name="Acme", slug="acme-cur")
        self.owner = User.objects.create_user(
            "cur@acme.io", "x", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=self.org, user=self.owner, role="owner")

    def set_to(self, code):
        return as_user(self.owner).patch(
            "/api/organisation", {"preferred_currency": code}, format="json")

    def test_a_supported_currency_is_accepted(self):
        for code in ("GBP", "eur", "NGN", "USD"):
            with self.subTest(code=code):
                self.assertEqual(self.set_to(code).status_code, 200)
                self.org.refresh_from_db()
                self.assertEqual(self.org.preferred_currency, code.upper())

    def test_an_unsupported_currency_is_refused(self):
        response = self.set_to("JPY")
        self.assertEqual(response.status_code, 400)
        self.assertIn("JPY", str(response.data))

    def test_blank_means_the_platform_default(self):
        self.set_to("GBP")
        self.assertEqual(self.set_to("").status_code, 200)
        self.org.refresh_from_db()
        self.assertEqual(self.org.preferred_currency, "")

    def test_only_an_owner_can_change_it(self):
        member = User.objects.create_user(
            "curmember@acme.io", "x", role=User.Role.CLIENT)
        OrganisationMember.objects.create(
            organisation=self.org, user=member, role="member")
        self.assertEqual(
            as_user(member).patch(
                "/api/organisation", {"preferred_currency": "GBP"},
                format="json").status_code, 403)
