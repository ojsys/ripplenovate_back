import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts import impersonation
from accounts.models import SiteSettings
from projects.access import can_access_project
from projects.models import Project

from . import earnings as earnings_service
from . import gateways, notifications, paystack, stripe_gateway, transfers
from . import refunds as refund_service
from .models import Earning, Payment, Refund, Withdrawal
from .serializers import (
    EarningSerializer,
    PayoutAccountSerializer,
    RefundCreateSerializer,
    RefundSerializer,
    ResolveAccountSerializer,
    TransferOtpSerializer,
    WithdrawalCreateSerializer,
    WithdrawalSerializer,
    WithdrawalSettleSerializer,
)

logger = logging.getLogger("ripple")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def initialize_payment(request, pk):
    project = Project.objects.filter(pk=pk).first()
    if not project:
        return Response({"detail": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
    if project.client_id != request.user.id:
        raise PermissionDenied("Only the project's client can pay this invoice.")
    if project.stage != Project.Stage.QUOTED:
        return Response(
            {"detail": "This invoice isn't ready for payment."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = gateways.initialize(project, request.user)
    except gateways.UnsupportedCurrency as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except (paystack.PaystackError, stripe_gateway.StripeError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(result)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def initialize_change_order_payment(request, pk):
    """The client paying for extra scope on a project already underway.

    A separate charge from the original invoice, deliberately: `quote_usd`
    locks at payment so the settled invoice and the snapshotted earnings can
    never disagree, and a change order adds contract value beside it rather
    than editing it.
    """
    from projects.models import ChangeOrder

    order = (ChangeOrder.objects.select_related("project")
             .filter(pk=pk).first())
    if not order:
        return Response({"detail": "Change order not found."},
                        status=status.HTTP_404_NOT_FOUND)
    if order.project.client_id != request.user.id:
        raise PermissionDenied("Only the project's client can pay for this.")
    if not order.is_payable:
        return Response(
            {"detail": f"This change order is already {order.get_status_display().lower()}."},
            status=status.HTTP_400_BAD_REQUEST)
    try:
        result = gateways.initialize(order.project, request.user, change_order=order)
    except gateways.UnsupportedCurrency as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except (paystack.PaystackError, stripe_gateway.StripeError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def verify_payment(request, reference):
    try:
        payment = gateways.verify(reference)
    except (paystack.PaystackError, stripe_gateway.StripeError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response({
        "reference": payment.reference,
        "status": payment.status,
        "project_id": payment.project_id,
        "project_stage": payment.project.stage,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def invoice(request, pk):
    """Invoice breakdown for the client's payment screen."""
    project = Project.objects.filter(pk=pk).first()
    if not project:
        return Response({"detail": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
    # Scoped to the project. The old check passed any delivery lead, which is
    # every lead on the platform — and the quote, the fee and the client are
    # nobody else's business.
    if not can_access_project(request.user, project):
        raise PermissionDenied("Not your invoice.")
    breakdown = paystack.quote_breakdown(project)
    currency, amount_subunit = paystack.charge_amount(breakdown["total_usd"])
    latest = project.payments.filter(status=Payment.Status.SUCCESS).first()
    return Response({
        "project_id": project.id,
        "code": project.code,
        "title": project.title,
        "category": project.category,
        "subtotal_usd": str(breakdown["subtotal_usd"]),
        "fee_usd": str(breakdown["fee_usd"]),
        "total_usd": str(breakdown["total_usd"]),
        "fee_percent": settings.PAYSTACK_FEE_PERCENT,
        "charge_currency": currency,
        "charge_amount_subunit": amount_subunit,
        "usd_to_ngn_rate": str(paystack.usd_to_ngn_rate()) if currency == "NGN" else None,
        "is_paid": project.is_paid,
        "public_key": settings.PAYSTACK_PUBLIC_KEY,
        "paid_reference": latest.reference if latest else None,
    })


def _require_earner_role(user):
    """Someone whose role earns — regardless of whether they're approved yet.

    Setting up *where* you would be paid is part of onboarding, and a partner
    fills it in while their application is still under review. Gating this on
    approval left the payout step of the wizard showing a spinner forever, so
    people skipped past it and arrived with no bank account on file.
    """
    if user.role not in (user.Role.EXPERT, user.Role.DELIVERY_LEAD,
                         user.Role.BUSINESS_DEV):
        raise PermissionDenied(
            "Payout accounts are for experts, delivery leads and business developers."
        )


def _require_earner(user):
    """Someone who may actually see or draw money. Approval matters here."""
    _require_earner_role(user)
    if user.needs_approval and not user.is_approved:
        raise PermissionDenied(
            "Your account is still being reviewed — earnings unlock once it's approved."
        )
    if not user.can_earn:
        raise PermissionDenied(
            "Earnings are for experts, delivery leads and business developers."
        )


def _require_settler(user):
    """Only an approved delivery lead / admin settles payouts — never their own."""
    if user.role != user.Role.DELIVERY_LEAD and not user.is_superuser:
        raise PermissionDenied("Only a delivery lead can settle payout requests.")
    if not user.is_approved:
        raise PermissionDenied("Your delivery lead account is still being reviewed.")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def earnings(request):
    """The signed-in earner's balance, ledger, and payout history."""
    user = request.user
    _require_earner(user)
    summary = earnings_service.summary(user)
    currency = settings.PAYSTACK_CURRENCY
    payload = {
        # Decimals as strings so no cent is lost to a float on the way out.
        **{key: str(value) for key, value in summary.items()},
        "payout_currency": currency,
        "usd_to_ngn_rate": str(paystack.usd_to_ngn_rate()) if currency == "NGN" else None,
        # The headline quotes the site default; this says whether it's the whole story.
        "has_custom_splits": earnings_service.has_custom_splits(user),
        "payout_account": _account_payload(user),
        "transfers_enabled": transfers.transfers_enabled(),
        "earnings": EarningSerializer(
            Earning.objects.filter(user=user).select_related("project"), many=True
        ).data,
        "withdrawals": WithdrawalSerializer(
            Withdrawal.objects.filter(user=user), many=True
        ).data,
    }
    if user.role == user.Role.DELIVERY_LEAD or user.is_superuser:
        # A lead also settles everyone else's requests (but never their own).
        queue = Withdrawal.objects.filter(
            status__in=Withdrawal.OPEN_STATUSES
        ).exclude(user=user).select_related("user")
        payload["payout_queue"] = WithdrawalSerializer(queue, many=True).data
    return Response(payload)


def _account_payload(user):
    return {
        "bank_name": user.bank_name,
        "bank_code": user.bank_code,
        "bank_account_number": user.bank_account_number,
        "bank_account_name": user.bank_account_name,
        "is_complete": user.has_payout_account,
    }


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def payout_account(request):
    """The signed-in earner's own bank account — read and update, nobody else's."""
    _require_earner_role(request.user)
    if request.method == "PUT":
        # Repointing where somebody's money lands, from inside their account,
        # is indistinguishable from them doing it. Reading it is fine — that's
        # most of what support needs.
        impersonation.forbid_while_impersonating(
            request, "Changing a payout account")
        serializer = PayoutAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        changed = (
            user.bank_code != data["bank_code"]
            or user.bank_account_number != data["bank_account_number"]
        )
        user.bank_name = data["bank_name"].strip()
        user.bank_code = data["bank_code"].strip()
        user.bank_account_number = data["bank_account_number"]
        user.bank_account_name = data["bank_account_name"].strip()
        if changed:
            # A new account needs a new Paystack recipient — never pay the old one.
            user.paystack_recipient_code = ""
        user.save(update_fields=[
            "bank_name", "bank_code", "bank_account_number", "bank_account_name",
            "paystack_recipient_code",
        ])
    return Response(_account_payload(request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payout_banks(request):
    """Banks Paystack can pay into, for the account picker."""
    _require_earner_role(request.user)
    try:
        return Response({"banks": transfers.list_banks()})
    except paystack.PaystackError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resolve_account(request):
    """Confirm an account number really belongs to the name on it."""
    _require_earner_role(request.user)
    serializer = ResolveAccountSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        result = transfers.resolve_account(
            serializer.validated_data["account_number"],
            serializer.validated_data["bank_code"],
        )
    except paystack.PaystackError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_withdrawal(request):
    """An expert or delivery lead withdraws part of their available balance."""
    _require_earner(request.user)
    impersonation.forbid_while_impersonating(request, "Requesting a withdrawal")
    serializer = WithdrawalCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        withdrawal = earnings_service.request_withdrawal(
            request.user, serializer.validated_data["amount_usd"]
        )
    except earnings_service.WithdrawalError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    notifications.notify_withdrawal_requested(withdrawal)
    return Response(
        WithdrawalSerializer(withdrawal).data, status=status.HTTP_201_CREATED
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def settle_withdrawal(request, pk):
    """Mark someone else's payout request processing / paid / rejected."""
    _require_settler(request.user)
    withdrawal = Withdrawal.objects.select_related("user").filter(pk=pk).first()
    if not withdrawal:
        return Response({"detail": "Withdrawal not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = WithdrawalSettleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        earnings_service.settle(
            withdrawal,
            serializer.validated_data["status"],
            request.user,
            serializer.validated_data.get("note", ""),
            manual=serializer.validated_data.get("manual", False),
        )
    except earnings_service.WithdrawalError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    # "Sending" isn't news for the earner — they hear when it lands or fails.
    if withdrawal.status != Withdrawal.Status.PROCESSING:
        notifications.notify_withdrawal_settled(withdrawal)
    return Response(WithdrawalSerializer(withdrawal).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sync_withdrawal(request, pk):
    """Re-check a payout with Paystack — for when a webhook hasn't landed."""
    _require_settler(request.user)
    withdrawal = Withdrawal.objects.select_related("user").filter(pk=pk).first()
    if not withdrawal:
        return Response({"detail": "Withdrawal not found."}, status=status.HTTP_404_NOT_FOUND)
    before = withdrawal.status
    try:
        transfers.verify(withdrawal)
    except paystack.PaystackError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if withdrawal.status != before and withdrawal.status != Withdrawal.Status.PROCESSING:
        notifications.notify_withdrawal_settled(withdrawal)
    return Response(WithdrawalSerializer(withdrawal).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def finalize_withdrawal(request, pk):
    """Supply the OTP when Paystack holds a transfer for confirmation."""
    _require_settler(request.user)
    withdrawal = Withdrawal.objects.select_related("user").filter(pk=pk).first()
    if not withdrawal:
        return Response({"detail": "Withdrawal not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = TransferOtpSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        transfers.finalize(withdrawal, serializer.validated_data["otp"])
    except paystack.PaystackError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if withdrawal.status != Withdrawal.Status.PROCESSING:
        notifications.notify_withdrawal_settled(withdrawal)
    return Response(WithdrawalSerializer(withdrawal).data)


@csrf_exempt
def paystack_webhook(request):
    """Paystack server-to-server events. Verifies the signature, then marks paid."""
    if request.method != "POST":
        return HttpResponse(status=405)

    signature = request.headers.get("x-paystack-signature", "")
    secret = settings.PAYSTACK_SECRET_KEY.encode()
    expected = hmac.new(secret, request.body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return HttpResponse(status=401)

    try:
        event = json.loads(request.body.decode())
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)

    name = event.get("event")
    data = event.get("data", {})

    if name == "charge.success":
        payment = Payment.objects.select_related("project", "project__client").filter(
            reference=data.get("reference")
        ).first()
        if payment:
            paystack._mark_paid(payment, data)

    # Payouts: Paystack's word is final on whether the money actually moved.
    elif name in ("transfer.success", "transfer.failed", "transfer.reversed"):
        withdrawal = transfers.handle_webhook_event(name, data)
        if withdrawal and withdrawal.status != Withdrawal.Status.PROCESSING:
            notifications.notify_withdrawal_settled(withdrawal)

    # Always 200 so Paystack stops retrying a handled event.
    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def _may_refund(user, project):
    """Who can put a refund on a project: its lead, or an admin."""
    if user.is_superuser:
        return True
    return (user.role == user.Role.DELIVERY_LEAD
            and project.lead_id == user.id
            and user.is_approved)


def _refund_payload(refund):
    return RefundSerializer(refund).data


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def project_refunds(request, pk):
    """List a project's refunds, or raise a new one.

    GET is open to anyone on the project — a client is entitled to see what
    they've been refunded without asking. POST is the delivery side only.
    """
    project = Project.objects.filter(pk=pk).first()
    if not project:
        return Response({"detail": "Project not found."},
                        status=status.HTTP_404_NOT_FOUND)
    if not can_access_project(request.user, project):
        raise PermissionDenied("Not your project.")

    if request.method == "GET":
        rows = project.refunds.select_related("requested_by", "approved_by")
        return Response({
            "refunds": RefundSerializer(rows, many=True).data,
            # What another refund would cost, so a lead sees the funding split
            # before committing rather than after.
            "collected_usd": str(project.collected_usd),
            "refunded_usd": str(project.refunded_usd),
            "refundable_usd": str(project.refundable_usd),
            "free_refund_usd": str(project.free_refund_usd),
            "admin_threshold_usd": str(
                SiteSettings.load().refund_admin_threshold_usd),
        })

    if not _may_refund(request.user, project):
        raise PermissionDenied(
            "Only this project's delivery lead or an administrator can refund it."
        )
    serializer = RefundCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        refund = refund_service.request_refund(
            project, request.user,
            serializer.validated_data["amount_usd"],
            serializer.validated_data["reason"],
        )
    except refund_service.RefundError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    # An approved refund still has to actually move; a requested one waits.
    if refund.status == Refund.Status.APPROVED:
        _send_refund(refund, request.user)
    notifications.notify_refund_raised(refund)
    return Response(_refund_payload(refund), status=status.HTTP_201_CREATED)


def _send_refund(refund, actor, manually=False):
    """Move an approved refund, or record it if transfers are off.

    Mirrors how payouts already work: when the gateway is disabled the platform
    is returning money by hand, and the ledger has to agree with reality either
    way.
    """
    payment = (refund.project.payments
               .filter(status=Payment.Status.SUCCESS)
               .order_by("-paid_at", "-id").first())
    if manually or not payment:
        # Nothing to reverse at a gateway — record it and let a human chase.
        return refund_service.settle(refund, manually=True)
    # Down the rail it came up. Re-routing a refund to today's default would
    # try to return money through a provider that never received it.
    if not gateways.refunds_enabled(payment):
        return refund_service.settle(refund, manually=True)
    try:
        raw = gateways.refund(payment, refund.amount_usd)
    except (paystack.PaystackError, stripe_gateway.StripeError) as exc:
        return refund_service.mark_failed(refund, str(exc))
    return refund_service.settle(
        refund, gateway=payment.gateway,
        gateway_reference=str(raw.get("id") or ""), gateway_raw=raw,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decide_refund(request, pk):
    """An administrator approving or rejecting a refund above the lead's limit."""
    refund = Refund.objects.select_related("project").filter(pk=pk).first()
    if not refund:
        return Response({"detail": "Refund not found."},
                        status=status.HTTP_404_NOT_FOUND)
    decision = (request.data.get("decision") or "").strip()
    if decision not in ("approve", "reject"):
        return Response({"detail": "Decide either approve or reject."},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        if decision == "approve":
            refund_service.approve_refund(refund, request.user)
            _send_refund(refund, request.user)
        else:
            refund_service.reject_refund(
                refund, request.user, request.data.get("reason", ""))
    except refund_service.RefundError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    notifications.notify_refund_decided(refund)
    return Response(_refund_payload(refund))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def refund_queue(request):
    """Refunds waiting on an administrator, plus the reserve's own position."""
    if not request.user.is_superuser:
        raise PermissionDenied("Only an administrator can see the refund queue.")
    pending = (Refund.objects
               .filter(status=Refund.Status.REQUESTED)
               .select_related("project", "requested_by"))
    return Response({
        "pending": RefundSerializer(pending, many=True).data,
        "reserve_balance_usd": str(refund_service.reserve_balance()),
        "reserve_percent": str(SiteSettings.load().reserve_percent),
    })


@csrf_exempt
def stripe_webhook(request):
    """Stripe server-to-server events.

    The signature check is the whole security model here: this URL is public,
    and without it anybody could mark their own project paid. It is done before
    the body is parsed, on the raw bytes, because that is what was signed.
    """
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        event = stripe_gateway.verify_webhook(
            request.body, request.headers.get("stripe-signature", ""))
    except stripe_gateway.StripeError as exc:
        logger.warning("Rejected a Stripe webhook: %s", exc)
        return HttpResponse(status=401)

    try:
        stripe_gateway.handle_event(event)
    except Exception as exc:  # noqa: BLE001
        # A 500 makes Stripe retry, which is right for a transient fault and
        # wrong for a poisoned event that will fail forever. Logged and
        # acknowledged; the verify-on-return path is the backstop.
        logger.error("Stripe event %s failed: %s", event.get("id"), exc)
    return HttpResponse(status=200)
