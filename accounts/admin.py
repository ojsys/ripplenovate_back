from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

from .models import EmailToken, SiteSettings, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "role", "is_email_verified", "is_staff")
    list_filter = ("role", "is_email_verified", "is_staff")
    search_fields = ("email", "full_name", "company")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "role", "company", "specialty", "active_load")}),
        ("Payout account", {
            "fields": ("bank_name", "bank_account_number", "bank_account_name"),
            "description": "Where withdrawals are paid. Developers and delivery leads "
                           "maintain this themselves when they withdraw earnings.",
        }),
        ("Status", {"fields": ("is_email_verified", "is_active", "is_staff", "is_superuser")}),
        ("Groups", {"fields": ("groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "full_name", "role", "password1", "password2"),
        }),
    )


@admin.register(EmailToken)
class EmailTokenAdmin(admin.ModelAdmin):
    list_display = ("token", "user", "purpose", "created_at", "used_at")
    list_filter = ("purpose",)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton: edit the one row; can't add more or delete it."""

    list_display = ("brand_name", "tagline", "usd_to_ngn_rate",
                    "developer_share_percent", "delivery_lead_share_percent",
                    "platform_share", "updated_at")
    readonly_fields = ("updated_at", "default_split")
    fieldsets = (
        ("Branding", {"fields": ("brand_name", "tagline")}),
        ("Payments", {
            "fields": ("usd_to_ngn_rate",),
            "description": "Quotes are fixed in USD. This rate converts the invoice "
                           "total to naira at the moment the client pays, so a saved "
                           "change applies to every unpaid invoice immediately.",
        }),
        ("Earnings & payouts", {
            "fields": ("default_split", "developer_share_percent",
                       "delivery_lead_share_percent", "min_withdrawal_usd"),
            "description": "The default split for every project's quote when the client "
                           "approves delivery. Whatever the developer and delivery lead "
                           "shares don't claim stays with the platform. A single project "
                           "can override these under Projects → Payout split. "
                           "Already-credited earnings keep the share they were created "
                           "with — changes apply to future approvals.",
        }),
        (None, {"fields": ("updated_at",)}),
    )

    @admin.display(description="Platform share (%)")
    def platform_share(self, obj):
        """The remainder — never stored, so the three shares always total 100%."""
        return obj.platform_share_percent

    @admin.display(description="Default split")
    def default_split(self, obj):
        return format_html(
            "Developer <strong>{}%</strong> · Delivery lead <strong>{}%</strong> · "
            "Platform <strong>{}%</strong> — of every quote.",
            obj.developer_share_percent,
            obj.delivery_lead_share_percent,
            obj.platform_share_percent,
        )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Ensure the row exists so the admin always shows something to edit.
        SiteSettings.load()
        return super().changelist_view(request, extra_context)
