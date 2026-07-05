from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({"service": "Ripple Innovation Labs API", "status": "ok"})


urlpatterns = [
    path("", health),
    path("admin/", admin.site.urls),
    path("api/", include("accounts.urls")),
    path("api/", include("projects.urls")),
    path("api/", include("payments.urls")),
]
