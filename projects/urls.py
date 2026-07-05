from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=False)
router.register("projects", views.ProjectViewSet, basename="project")

urlpatterns = [
    path("projects/stats/admin", views.admin_stats),
    path("tasks/<int:task_id>/toggle", views.toggle_task),
    *router.urls,
]
