from django.urls import path

from . import views

urlpatterns = [
    path("projects/<int:pk>/invoice", views.invoice),
    path("projects/<int:pk>/pay/initialize", views.initialize_payment),
    path("payments/verify/<str:reference>", views.verify_payment),
    path("earnings", views.earnings),
    path("payouts/account", views.payout_account),
    path("payouts/banks", views.payout_banks),
    path("payouts/resolve", views.resolve_account),
    path("withdrawals", views.create_withdrawal),
    path("withdrawals/<int:pk>/settle", views.settle_withdrawal),
    path("withdrawals/<int:pk>/sync", views.sync_withdrawal),
    path("withdrawals/<int:pk>/finalize", views.finalize_withdrawal),
    path("paystack/webhook", views.paystack_webhook),
]
