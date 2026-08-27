from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=False)
router.register("projects", views.ProjectViewSet, basename="project")

urlpatterns = [
    path("projects/stats/admin", views.admin_stats),
    path("pipeline", views.pipeline),
    path("reviews", views.reviews),
    path("leaderboard", views.leaderboard),
    path("request-lead", views.request_lead),
    # A client asking for a different delivery lead, and the admin queue.
    path("projects/<int:pk>/lead-change", views.request_lead_change),
    path("lead-changes", views.lead_change_queue),
    path("lead-changes/<int:pk>/resolve", views.resolve_lead_change),
    # Retainers, and the utilisation figures behind them.
    path("engagements", views.engagements),
    path("engagements/<int:engagement_id>", views.engagement_detail),
    path("utilisation", views.utilisation),
    path("reports", views.reports),
    path("analytics", views.analytics),
    path("tasks/<int:task_id>", views.task_detail),
    path("tasks/<int:task_id>/reassign", views.reassign_task),
    path("tasks/<int:task_id>/submit", views.submit_task),
    path("tasks/<int:task_id>/approve", views.approve_task),
    path("tasks/<int:task_id>/request-changes", views.request_task_changes),
    path("change-orders/<int:order_id>", views.withdraw_change_order),
    path("attachments/<int:attachment_id>", views.delete_attachment),
    path("attachments/<int:attachment_id>/download", views.download_attachment),
    *router.urls,
]


