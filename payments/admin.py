from django.contrib import admin, messages

from . import earnings as earnings_service
from . import notifications, transfers
from .models import Earning, Payment, Withdrawal
from .paystack import PaystackError


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
                    "bank_name", "masked_account", "transfer_reference",
                    "created_at", "processed_at")
    list_filter = ("status", "currency")
    search_fields = ("reference", "user__email", "user__full_name", "bank_account_number",
                     "transfer_reference")
    readonly_fields = ("reference", "user", "amount_usd", "currency", "amount_subunit",
                       "usd_to_ngn_rate", "bank_name", "bank_code", "bank_account_number",
                       "bank_account_name", "recipient_code", "transfer_code",
                       "transfer_reference", "transfer_raw", "failure_reason",
                       "processed_by", "processed_at", "created_at")
    date_hierarchy = "created_at"
    actions = ("send_payout", "mark_paid_manually", "mark_rejected", "recheck_with_paystack")

    @admin.display(description="Account")
    def masked_account(self, obj):
        return obj.masked_account

    def _settle_selected(self, request, queryset, status, manual=False, verb=None):
        settled, skipped = 0, []
        for withdrawal in queryset:
            try:
                earnings_service.settle(withdrawal, status, request.user, manual=manual)
            except earnings_service.WithdrawalError as exc:
                skipped.append(f"{withdrawal.reference}: {exc}")
                continue
            if withdrawal.status != Withdrawal.Status.PROCESSING:
                notifications.notify_withdrawal_settled(withdrawal)
            settled += 1
        if settled:
            self.message_user(request, f"{settled} payout(s) {verb or status}.")
        for problem in skipped:
            self.message_user(request, problem, level=messages.WARNING)

    @admin.action(description="Approve & send payout via Paystack (moves money)")
    def send_payout(self, request, queryset):
        self._settle_selected(request, queryset, Withdrawal.Status.PAID,
                              verb="sent to Paystack")

    @admin.action(description="Record as paid manually (no Paystack transfer)")
    def mark_paid_manually(self, request, queryset):
        self._settle_selected(request, queryset, Withdrawal.Status.PAID, manual=True,
                              verb="recorded as paid")

    @admin.action(description="Reject selected payouts (frees up the funds)")
    def mark_rejected(self, request, queryset):
        self._settle_selected(request, queryset, Withdrawal.Status.REJECTED,
                              verb="rejected")

    @admin.action(description="Re-check status with Paystack")
    def recheck_with_paystack(self, request, queryset):
        checked, problems = 0, []
        for withdrawal in queryset:
            try:
                transfers.verify(withdrawal)
            except PaystackError as exc:
                problems.append(f"{withdrawal.reference}: {exc}")
                continue
            checked += 1
        if checked:
            self.message_user(request, f"{checked} payout(s) re-checked with Paystack.")
        for problem in problems:
            self.message_user(request, problem, level=messages.WARNING)
