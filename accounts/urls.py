from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("site-settings", views.site_settings),
    path("auth/register", views.register),
    path("auth/login", views.login),
    path("auth/token/refresh", TokenRefreshView.as_view()),
    path("auth/verify-email", views.verify_email),
    path("auth/resend-verification", views.resend_verification),
    path("auth/password-reset/request", views.password_reset_request),
    path("auth/password-reset/confirm", views.password_reset_confirm),
    path("auth/me", views.me),
    path("auth/change-password", views.change_password),
    path("users/experts", views.experts),
    path("users/business-developers", views.business_developers),
    path("users/<int:user_id>", views.update_expert),
    path("users/<int:user_id>/role", views.update_role),
    # Partner onboarding & approval
    path("onboarding", views.onboarding),
    path("onboarding/submit", views.submit_application),
    path("applications", views.applications),
    path("applications/<int:user_id>/decide", views.decide_application),
    # Expert invitations
    path("invitations", views.invitations),
    path("invitations/<int:invitation_id>/revoke", views.revoke_invitation),
    path("invitations/<int:invitation_id>/resend", views.resend_invitation),
    path("invite/<uuid:token>", views.invitation_detail),
    path("invite/<uuid:token>/accept", views.accept_invitation),
]
