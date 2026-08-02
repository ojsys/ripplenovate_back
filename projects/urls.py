from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=False)
router.register("projects", views.ProjectViewSet, basename="project")

urlpatterns = [
    path("projects/stats/admin", views.admin_stats),
    path("pipeline", views.pipeline),
    path("reports", views.reports),
    path("tasks/<int:task_id>/toggle", views.toggle_task),
    path("attachments/<int:attachment_id>", views.delete_attachment),
    *router.urls,
]
