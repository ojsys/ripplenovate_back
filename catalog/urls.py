from django.urls import path

from . import views

urlpatterns = [
    path("product-lines", views.product_lines),
]
