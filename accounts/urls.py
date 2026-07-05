from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("auth/register", views.register),
    path("auth/login", views.login),
    path("auth/token/refresh", TokenRefreshView.as_view()),
    path("auth/verify-email", views.verify_email),
    path("auth/resend-verification", views.resend_verification),
    path("auth/password-reset/request", views.password_reset_request),
    path("auth/password-reset/confirm", views.password_reset_confirm),
    path("auth/me", views.me),
    path("auth/change-password", views.change_password),
    path("users/developers", views.developers),
    path("users/<int:user_id>", views.update_developer),
    path("users/<int:user_id>/role", views.update_role),
]
