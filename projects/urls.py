from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=False)
router.register("projects", views.ProjectViewSet, basename="project")

urlpatterns = [
    path("projects/stats/admin", views.admin_stats),
    path("pipeline", views.pipeline),
    path("reports", views.reports),
    path("analytics", views.analytics),
    path("tasks/<int:task_id>", views.task_detail),
    path("tasks/<int:task_id>/reassign", views.reassign_task),
    path("tasks/<int:task_id>/submit", views.submit_task),
    path("tasks/<int:task_id>/approve", views.approve_task),
    path("tasks/<int:task_id>/request-changes", views.request_task_changes),
    path("attachments/<int:attachment_id>", views.delete_attachment),
    path("attachments/<int:attachment_id>/download", views.download_attachment),
    *router.urls,
]
