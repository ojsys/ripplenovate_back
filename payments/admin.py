from django.contrib import admin, messages

from . import earnings as earnings_service
from . import notifications
from .models import Earning, Payment, Withdrawal


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "project", "usd_total", "amount_subunit", "currency", "usd_to_ngn_rate", "status", "paid_at", "created_at")
    list_filter = ("status", "currency")
    search_fields = ("reference", "project__code", "project__title")
    readonly_fields = ("created_at", "paid_at", "raw", "usd_to_ngn_rate")
    date_hierarchy = "created_at"


@admin.register(Earning)
class EarningAdmin(admin.ModelAdmin):
    """Read-only: rows are written by the delivery lifecycle, never by hand."""

    list_display = ("user", "project", "kind", "share_percent", "amount_usd", "created_at")
    list_filter = ("kind",)
    search_fields = ("user__email", "user__full_name", "project__code", "project__title")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


@admin.register(Withdrawal)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = ("reference", "user", "amount_usd", "currency", "status",
                    "bank_name", "masked_account", "created_at", "processed_at")
    list_filter = ("status", "currency")
    search_fields = ("reference", "user__email", "user__full_name", "bank_account_number")
    readonly_fields = ("reference", "user", "amount_usd", "currency", "amount_subunit",
                       "usd_to_ngn_rate", "bank_name", "bank_account_number",
                       "bank_account_name", "processed_by", "processed_at", "created_at")
    date_hierarchy = "created_at"
    actions = ("mark_paid", "mark_rejected")

    @admin.display(description="Account")
    def masked_account(self, obj):
        return obj.masked_account

    def _settle_selected(self, request, queryset, status):
        settled, skipped = 0, []
        for withdrawal in queryset:
            try:
                earnings_service.settle(withdrawal, status, request.user)
            except earnings_service.WithdrawalError as exc:
                skipped.append(f"{withdrawal.reference}: {exc}")
                continue
            notifications.notify_withdrawal_settled(withdrawal)
            settled += 1
        if settled:
            self.message_user(request, f"{settled} withdrawal(s) marked {status}.")
        for problem in skipped:
            self.message_user(request, problem, level=messages.WARNING)

    @admin.action(description="Mark selected withdrawals as paid")
    def mark_paid(self, request, queryset):
        self._settle_selected(request, queryset, Withdrawal.Status.PAID)

    @admin.action(description="Reject selected withdrawals (frees up the funds)")
    def mark_rejected(self, request, queryset):
        self._settle_selected(request, queryset, Withdrawal.Status.REJECTED)
