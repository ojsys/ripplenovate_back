from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

# Branding shown in the admin (titles, breadcrumbs, login header).
admin.site.site_header = "Ripple Innovation Labs"
admin.site.site_title = "Ripple Innovation Labs"
admin.site.index_title = "Console"


def health(_request):
    return JsonResponse({"service": "Ripple Innovation Labs API", "status": "ok"})


urlpatterns = [
    path("", health),
    path("admin/", admin.site.urls),
    path("api/", include("accounts.urls")),
    path("api/", include("projects.urls")),
    path("api/", include("payments.urls")),
]
