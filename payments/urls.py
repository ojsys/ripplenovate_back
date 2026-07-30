from django.urls import path

from . import views

urlpatterns = [
    path("projects/<int:pk>/invoice", views.invoice),
    path("projects/<int:pk>/pay/initialize", views.initialize_payment),
    path("payments/verify/<str:reference>", views.verify_payment),
    path("earnings", views.earnings),
    path("withdrawals", views.create_withdrawal),
    path("withdrawals/<int:pk>/settle", views.settle_withdrawal),
    path("paystack/webhook", views.paystack_webhook),
]
