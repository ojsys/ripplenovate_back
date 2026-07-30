import hashlib
import hmac
import json

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from projects.models import Project

from . import earnings as earnings_service
from . import notifications, paystack
from .models import Earning, Payment, Withdrawal
from .serializers import (
    EarningSerializer,
    WithdrawalCreateSerializer,
    WithdrawalSerializer,
    WithdrawalSettleSerializer,
)


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
        result = paystack.initialize(project, request.user)
    except paystack.PaystackError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def verify_payment(request, reference):
    try:
        payment = paystack.verify(reference)
    except paystack.PaystackError as exc:
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
    if project.client_id != request.user.id and request.user.role != request.user.Role.DELIVERY_LEAD:
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


def _require_earner(user):
    if not user.can_earn:
        raise PermissionDenied("Earnings are for developers and delivery leads.")


def _require_settler(user):
    """Only a delivery lead / admin settles payout requests — never their own."""
    if user.role != user.Role.DELIVERY_LEAD and not user.is_superuser:
        raise PermissionDenied("Only a delivery lead can settle payout requests.")


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
        "payout_account": {
            "bank_name": user.bank_name,
            "bank_account_number": user.bank_account_number,
            "bank_account_name": user.bank_account_name,
        },
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_withdrawal(request):
    """A developer or delivery lead withdraws part of their available balance."""
    _require_earner(request.user)
    serializer = WithdrawalCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        withdrawal = earnings_service.request_withdrawal(
            request.user,
            serializer.validated_data["amount_usd"],
            serializer.validated_data["bank_name"],
            serializer.validated_data["bank_account_number"],
            serializer.validated_data["bank_account_name"],
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
        )
    except earnings_service.WithdrawalError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
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

    if event.get("event") == "charge.success":
        reference = event.get("data", {}).get("reference")
        payment = Payment.objects.select_related("project", "project__client").filter(
            reference=reference
        ).first()
        if payment:
            paystack._mark_paid(payment, event.get("data", {}))

    # Always 200 so Paystack stops retrying a handled event.
    return HttpResponse(status=200)
