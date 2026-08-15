from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

from django.utils import timezone

from .models import (
    EmailToken,
    ImpersonationEvent,
    Invitation,
    KycProfile,
    ProfessionalProfile,
    SiteSettings,
    User,
)


class ProfessionalProfileInline(admin.StackedInline):
    model = ProfessionalProfile
    extra = 0
    can_delete = False
    verbose_name_plural = "Professional profile"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "role", "approval_status",
                    "is_email_verified", "is_staff")
    list_filter = ("role", "approval_status", "is_email_verified", "is_staff")
    search_fields = ("email", "full_name", "company")
    inlines = [ProfessionalProfileInline]
    actions = ["approve_applications", "reject_applications"]

    @admin.action(description="Approve selected partner applications")
    def approve_applications(self, request, queryset):
        """Approve delivery leads / business developers, and send their welcome."""
        from .emails import send_business_dev_welcome, send_delivery_lead_welcome

        approved = 0
        for user in queryset.filter(role__in=User.APPROVAL_ROLES):
            if user.approval_status == User.ApprovalStatus.APPROVED:
                continue
            user.approve(by=request.user)
            if user.role == User.Role.BUSINESS_DEV:
                user.ensure_referral_code()
                send_business_dev_welcome(user)
            else:
                send_delivery_lead_welcome(user)
            approved += 1
        self.message_user(request, f"Approved {approved} application(s).")

    @admin.action(description="Decline selected partner applications")
    def reject_applications(self, request, queryset):
        """Decline without a reason. Use the API or edit the row to add one —
        the reason is shown to the applicant, so it's worth writing."""
        from .emails import send_application_rejected

        rejected = 0
        for user in queryset.filter(role__in=User.APPROVAL_ROLES):
            user.reject(by=request.user)
            send_application_rejected(user)
            rejected += 1
        self.message_user(request, f"Declined {rejected} application(s).")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "role", "company", "specialty", "active_load")}),
        ("Payout account", {
            "fields": ("bank_name", "bank_code", "bank_account_number",
                       "bank_account_name", "paystack_recipient_code"),
            "description": "Where withdrawals are paid. Experts and delivery leads "
                           "maintain this in their own profile settings; the bank code "
                           "is Paystack's, and the recipient code is created on the "
                           "first payout. Clear the recipient code to force it to be "
                           "recreated.",
        }),
        ("Team & disciplines", {
            "fields": ("product_lines", "skills", "lead"),
            "description": "Which lines this person works in, and (for an expert) "
                           "the delivery lead they sit under.",
        }),
        ("Business development", {
            "fields": ("referral_code", "referred_by"),
            "description": "A business developer's shareable code, and — for a "
                           "client — who referred them. Every project a referred "
                           "client posts carries that BD's commission.",
        }),
        ("Application", {
            "fields": ("approval_status", "applied_at", "approved_at", "approved_by",
                       "rejection_reason", "onboarding_step", "onboarding_completed_at"),
            "description": "Delivery leads and business developers can't quote work "
                           "or be paid until approved. The rejection reason is emailed "
                           "to the applicant, so write it for them to read.",
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


@admin.register(ImpersonationEvent)
class ImpersonationEventAdmin(admin.ModelAdmin):
    """Read-only on purpose. An audit trail an admin can edit isn't one."""

    list_display = ("started_at", "impersonator", "target", "ended_at",
                    "ip_address", "reason")
    list_filter = ("started_at",)
    search_fields = ("impersonator__email", "target__email", "reason")
    readonly_fields = ("impersonator", "target", "reason", "started_at",
                       "ended_at", "ip_address", "user_agent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton: edit the one row; can't add more or delete it."""

    list_display = ("brand_name", "tagline", "usd_to_ngn_rate",
                    "expert_share_percent", "delivery_lead_share_percent",
                    "business_dev_share_percent", "platform_share", "updated_at")
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
            "fields": ("default_split", "expert_share_percent",
                       "delivery_lead_share_percent", "business_dev_share_percent",
                       "min_withdrawal_usd", "require_kyc_for_payout"),
            "description": "The default split for every project's quote when the client "
                           "approves delivery. Whatever the others don't claim stays with "
                           "the platform. The business developer commission is charged "
                           "only on projects that have one attributed — a direct project "
                           "keeps it in the platform's share. A single project can "
                           "override these under Projects → Payout split. Already-credited "
                           "earnings keep the share they were created with — changes apply "
                           "to future approvals.",
        }),
        (None, {"fields": ("updated_at",)}),
    )

    @admin.display(description="Platform share (%)")
    def platform_share(self, obj):
        """The remainder — never stored, so the shares always total 100%.

        Shown as a range because it depends on whether a project was sourced by
        a business developer.
        """
        sourced = obj.platform_share_percent
        direct = obj.platform_share_direct_percent
        if sourced == direct:
            return direct
        return f"{sourced}–{direct}"

    @admin.display(description="Default split")
    def default_split(self, obj):
        return format_html(
            "Expert <strong>{}%</strong> · Delivery lead <strong>{}%</strong> · "
            "Business developer <strong>{}%</strong> · Platform <strong>{}%</strong>"
            " on a sourced project, <strong>{}%</strong> on a direct one.",
            obj.expert_share_percent,
            obj.delivery_lead_share_percent,
            obj.business_dev_share_percent,
            obj.platform_share_percent,
            obj.platform_share_direct_percent,
        )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Ensure the row exists so the admin always shows something to edit.
        SiteSettings.load()
        return super().changelist_view(request, extra_context)


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "full_name", "invited_by", "status", "expiry",
                    "created_at", "accepted_at")
    list_filter = ("status", "product_lines")
    search_fields = ("email", "full_name", "invited_by__email")
    readonly_fields = ("token", "created_at", "accepted_at")
    autocomplete_fields = ("invited_by",)

    @admin.display(description="Expires")
    def expiry(self, obj):
        if obj.status != Invitation.Status.PENDING:
            return "—"
        if obj.is_expired:
            return "Expired"
        days = (obj.expires_at - timezone.now()).days
        return f"in {days} day{'s' if days != 1 else ''}"


@admin.register(ProfessionalProfile)
class ProfessionalProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "country", "years_experience", "has_cv", "updated_at")
    search_fields = ("user__email", "user__full_name", "country")
    autocomplete_fields = ("user",)

    @admin.display(description="CV", boolean=True)
    def has_cv(self, obj):
        return bool(obj.cv)


@admin.register(KycProfile)
class KycProfileAdmin(admin.ModelAdmin):
    """Identity records.

    The raw ID number is intentionally read-only here rather than editable: an
    admin's job is to check the typed details against the uploaded document, not
    to retype someone's passport number. Restrict this model's permission to the
    people who actually do verification.
    """

    list_display = ("user", "status", "legal_name", "country", "id_type",
                    "masked_id", "submitted_at", "reviewed_by")
    list_filter = ("status", "id_type", "country")
    search_fields = ("user__email", "user__full_name", "legal_name")
    autocomplete_fields = ("user", "reviewed_by")
    readonly_fields = ("masked_id", "submitted_at", "reviewed_at", "created_at",
                       "updated_at")
    actions = ["mark_verified", "mark_rejected"]
    fieldsets = (
        ("Person", {"fields": ("user", "legal_name", "date_of_birth", "phone")}),
        ("Address", {
            "fields": ("address_line1", "address_line2", "city", "state",
                       "postal_code", "country"),
        }),
        ("Identity document", {
            "fields": ("id_type", "id_number", "masked_id", "id_document", "tax_id"),
            "description": "Check the typed details against the uploaded document. "
                           "The document is downloaded through an authenticated "
                           "view — it is never served from a public URL.",
        }),
        ("Review", {
            "fields": ("status", "submitted_at", "reviewed_at", "reviewed_by",
                       "rejection_reason"),
            "description": "The rejection reason is emailed to the person, so write "
                           "it for them to read — usually a blurred photo or a name "
                           "that doesn't match.",
        }),
    )

    @admin.display(description="ID number")
    def masked_id(self, obj):
        return obj.masked_id_number or "—"

    @admin.action(description="Mark selected as verified")
    def mark_verified(self, request, queryset):
        from .emails import send_kyc_verified

        count = 0
        for kyc in queryset.exclude(status=KycProfile.Status.VERIFIED):
            kyc.verify(by=request.user)
            send_kyc_verified(kyc.user)
            count += 1
        self.message_user(request, f"Verified {count} record(s).")

    @admin.action(description="Mark selected as rejected")
    def mark_rejected(self, request, queryset):
        from .emails import send_kyc_rejected

        count = 0
        for kyc in queryset.exclude(status=KycProfile.Status.REJECTED):
            kyc.reject(by=request.user)
            send_kyc_rejected(kyc.user)
            count += 1
        self.message_user(request, f"Rejected {count} record(s).")
