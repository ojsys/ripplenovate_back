from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import EmailToken, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "role", "is_email_verified", "is_staff")
    list_filter = ("role", "is_email_verified", "is_staff")
    search_fields = ("email", "full_name", "company")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "role", "company", "specialty", "active_load")}),
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
