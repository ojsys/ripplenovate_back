from django.contrib import admin

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "project", "usd_total", "amount_subunit", "currency", "usd_to_ngn_rate", "status", "paid_at", "created_at")
    list_filter = ("status", "currency")
    search_fields = ("reference", "project__code", "project__title")
    readonly_fields = ("created_at", "paid_at", "raw", "usd_to_ngn_rate")
    date_hierarchy = "created_at"
