from django.contrib import admin

from .models import ProductLine, Service


class ServiceInline(admin.TabularInline):
    model = Service
    extra = 1
    fields = ("name", "description", "typical_timeline", "order", "is_active")


@admin.register(ProductLine)
class ProductLineAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "service_count",
                    "lead_count", "expert_count", "project_count", "order")
    list_filter = ("is_active",)
    list_editable = ("is_active", "order")
    search_fields = ("name", "slug", "tagline")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [ServiceInline]
    fieldsets = (
        (None, {"fields": ("name", "slug", "tagline", "description")}),
        ("Presentation", {
            "fields": ("accent", "icon", "order"),
            "description": "How the line looks on the client's brief form.",
        }),
        ("Availability", {
            "fields": ("is_active",),
            "description": "Turn a line off to stop new briefs coming into it. "
                           "Existing projects keep their line either way — never "
                           "delete a line that has delivered work.",
        }),
    )

    @admin.display(description="Services")
    def service_count(self, obj):
        return obj.services.count()

    @admin.display(description="Leads")
    def lead_count(self, obj):
        return obj.members.filter(role="delivery_lead").count()

    @admin.display(description="Experts")
    def expert_count(self, obj):
        return obj.members.filter(role="expert").count()

    @admin.display(description="Projects")
    def project_count(self, obj):
        return obj.projects.count()


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "product_line", "typical_timeline", "is_active", "order")
    list_filter = ("product_line", "is_active")
    search_fields = ("name",)
