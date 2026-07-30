from django.contrib import admin
from django.utils.html import format_html, format_html_join

from .models import Activity, Project, Task


def _usd(amount):
    return "${:,.2f}".format(amount or 0)


class TaskInline(admin.TabularInline):
    model = Task
    extra = 0
    fields = ("title", "assignee", "done", "order")


class ActivityInline(admin.TabularInline):
    model = Activity
    extra = 0
    fields = ("kind", "author_name", "role_label", "text", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "company", "category", "stage", "quote_usd", "developer", "lead", "created_at")
    list_filter = ("stage", "category")
    search_fields = ("code", "title", "company", "client__full_name", "client__email")
    autocomplete_fields = ("client", "developer", "lead")
    readonly_fields = ("code", "created_at", "payout_breakdown")
    date_hierarchy = "created_at"
    inlines = [TaskInline, ActivityInline]
    fieldsets = (
        (None, {"fields": ("code", "title", "client", "company", "category")}),
        ("Brief", {"fields": ("description", "timeline", "budget_range", "target_date")}),
        ("Delivery", {"fields": ("stage", "quote_usd", "developer", "lead")}),
        ("Payout split", {
            "fields": ("payout_breakdown", "developer_share_percent",
                       "delivery_lead_share_percent"),
            "description": "How this project's quote is divided. Both percentages are "
                           "optional — leave them blank to follow the site defaults in "
                           "Site settings, or set them to price one project differently. "
                           "Whatever the two shares don't claim stays with the platform.",
        }),
        ("Timestamps", {"fields": ("created_at",)}),
    )

    @admin.display(description="Where the quote goes")
    def payout_breakdown(self, obj):
        """Spell out the split — including the platform's remainder — in the form."""
        if obj is None or not obj.pk:
            return "Save the project to see its payout split."
        split = obj.payout_split()
        if not split["quote_usd"]:
            return "No quote yet, so there's nothing to split."

        rows = [
            ("Developer", split["developer_percent"], split["developer_usd"],
             obj.developer.full_name if obj.developer else "unassigned"),
            ("Delivery lead", split["delivery_lead_percent"], split["delivery_lead_usd"],
             obj.lead.full_name if obj.lead else "unassigned"),
            ("Platform", split["platform_percent"], split["platform_usd"],
             "Ripple Innovation Labs"),
        ]
        body = format_html_join(
            "",
            '<tr><th style="text-align:left;padding:6px 14px 6px 0;font-weight:600">{}</th>'
            '<td style="padding:6px 14px 6px 0;white-space:nowrap">{}%</td>'
            '<td style="padding:6px 14px 6px 0;white-space:nowrap;font-weight:600">{}</td>'
            '<td style="padding:6px 0;color:#8B93A0">{}</td></tr>',
            ((label, pct, _usd(usd), who) for label, pct, usd, who in rows),
        )
        note = (
            "Credited — these are the amounts actually paid out, snapshotted when the "
            "client approved delivery."
            if split["is_settled"] else
            "Projected — the shares will be credited when the client approves delivery."
        )
        if split["uses_override"]:
            note += " This project overrides the site defaults."
        return format_html(
            '<table style="border-collapse:collapse;margin-bottom:8px">'
            '<tr><th style="text-align:left;padding:0 14px 8px 0;color:#8B93A0;'
            'font-weight:600;font-size:11px;letter-spacing:.05em">SHARE</th>'
            '<th style="text-align:left;padding:0 14px 8px 0;color:#8B93A0;font-weight:600;'
            'font-size:11px;letter-spacing:.05em">%</th>'
            '<th style="text-align:left;padding:0 14px 8px 0;color:#8B93A0;font-weight:600;'
            'font-size:11px;letter-spacing:.05em">OF {}</th>'
            '<th style="text-align:left;padding:0 0 8px;color:#8B93A0;font-weight:600;'
            'font-size:11px;letter-spacing:.05em">WHO</th></tr>{}</table>'
            '<div style="color:#8B93A0;font-size:12.5px">{}</div>',
            _usd(split["quote_usd"]), body, note,
        )


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "assignee", "done", "order")
    list_filter = ("done",)
    search_fields = ("title", "project__code", "project__title")


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("project", "kind", "author_name", "role_label", "created_at")
    list_filter = ("kind", "role_label")
    search_fields = ("text", "author_name", "project__code")
    readonly_fields = ("created_at",)
