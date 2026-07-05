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

from . import paystack
from .models import Payment


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
        "is_paid": project.is_paid,
        "public_key": settings.PAYSTACK_PUBLIC_KEY,
        "paid_reference": latest.reference if latest else None,
    })


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
