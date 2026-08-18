from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .models import Activity, Attachment, CycleRun, Engagement, Project, Task


def _usd(amount):
    return "${:,.2f}".format(amount or 0)


def _pct(value):
    """60.00 → '60%', 12.50 → '12.5%' — compact enough for a table column."""
    return "{}%".format(("%f" % value).rstrip("0").rstrip("."))


class TaskInline(admin.TabularInline):
    model = Task
    extra = 0
    fields = ("title", "assignee", "amount_usd", "status", "order")


class ActivityInline(admin.TabularInline):
    model = Activity
    extra = 0
    fields = ("kind", "author_name", "role_label", "text", "created_at")
    readonly_fields = ("created_at",)


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0
    fields = ("url", "label", "kind", "purpose", "added_by", "created_at")
    readonly_fields = ("kind", "created_at")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "company", "category", "stage", "quote_usd",
                    "payout_split_column", "expert", "lead", "target_date",
                    "delivery_flag", "created_at")
    list_filter = ("stage", "category", "product_line")
    search_fields = ("code", "title", "company", "client__full_name", "client__email")
    autocomplete_fields = ("client", "expert", "lead", "business_developer")
    filter_horizontal = ("experts",)
    readonly_fields = ("code", "created_at", "payout_breakdown")
    date_hierarchy = "created_at"
    inlines = [TaskInline, AttachmentInline, ActivityInline]
    fieldsets = (
        (None, {"fields": ("code", "title", "client", "company", "category")}),
        ("Brief", {"fields": ("description", "timeline", "budget_range", "target_date")}),
        ("Delivery", {
            "fields": ("stage", "quote_usd", "expert", "experts", "lead",
                       "business_developer"),
            "description": "“Primary expert” owns the brief and is who a project "
                           "with no priced tasks pays in full. Keep them in the "
                           "team list as well.",
        }),
        ("Payout split", {
            "fields": ("payout_breakdown", "expert_share_percent",
                       "business_dev_share_percent",
                       "delivery_lead_share_percent"),
            "description": "How this project's quote is divided. Both percentages are "
                           "optional — leave them blank to follow the site defaults in "
                           "Site settings, or set them to price one project differently. "
                           "Whatever the two shares don't claim stays with the platform.",
        }),
        ("Timestamps", {"fields": ("created_at",)}),
    )

    def get_queryset(self, request):
        # payout_split() reads credited earnings for delivered projects.
        return super().get_queryset(request).prefetch_related("earnings")

    @admin.display(description="Delivery")
    def delivery_flag(self, obj):
        """On time / late / overdue at a glance, with no date treated as a miss."""
        if obj.is_on_time is True:
            return format_html('<span style="color:#0B7D61">On time</span>')
        if obj.is_on_time is False:
            return format_html(
                '<span style="color:#C2410C">{} day{} late</span>',
                obj.days_late, "" if obj.days_late == 1 else "s")
        if obj.is_overdue:
            return format_html('<span style="color:#C2410C;font-weight:600">Overdue</span>')
        return "—"

    @admin.display(description="Split expert/lead/BD/platform")
    def payout_split_column(self, obj):
        """At-a-glance split, so the platform's cut is visible from the list."""
        split = obj.payout_split()
        label = "{}/{}/{}/{}".format(
            _pct(split["expert_percent"]),
            _pct(split["delivery_lead_percent"]),
            _pct(split["business_dev_percent"]),
            _pct(split["platform_percent"]),
        )
        if split["uses_override"]:
            return format_html(
                '<span title="This project overrides the site defaults" '
                'style="color:#0B7D61;font-weight:600">{} ★</span>', label)
        return label

    @admin.display(description="Where the quote goes")
    def payout_breakdown(self, obj):
        """The full split — expert, lead, business developer, and the remainder.

        On an unsettled project the figures recalculate as you type the
        percentages, so the platform's share is visible before you save.
        """
        if obj is None or not obj.pk:
            return "Save the project to see its payout split."
        split = obj.payout_split()

        rows = [
            ("expert", "Expert", split["expert_percent"], split["expert_usd"],
             obj.expert.full_name if obj.expert else "unassigned"),
            ("lead", "Delivery lead", split["delivery_lead_percent"], split["delivery_lead_usd"],
             obj.lead.full_name if obj.lead else "unassigned"),
            ("bizdev", "Business developer", split["business_dev_percent"],
             split["business_dev_usd"],
             obj.business_developer.full_name if obj.business_developer
             else "none — the platform keeps this"),
            ("platform", "Platform", split["platform_percent"], split["platform_usd"],
             "Ripple Innovation Labs"),
        ]
        body = format_html_join(
            "",
            '<tr data-role="{}"><th style="text-align:left;padding:6px 16px 6px 0;'
            'font-weight:600;white-space:nowrap">{}</th>'
            '<td data-cell="pct" style="padding:6px 16px 6px 0;white-space:nowrap">{}%</td>'
            '<td data-cell="usd" style="padding:6px 16px 6px 0;white-space:nowrap;'
            'font-weight:600">{}</td>'
            '<td style="padding:6px 0;color:#8B93A0;white-space:nowrap">{}</td></tr>',
            ((key, label, pct, _usd(usd), who) for key, label, pct, usd, who in rows),
        )
        if split["is_settled"]:
            note = ("Credited — these are the amounts actually paid out, snapshotted when "
                    "the client approved delivery, so editing the percentages below no "
                    "longer changes them.")
        elif not split["quote_usd"]:
            note = ("No quote yet — the amounts fill in once this project is quoted. The "
                    "percentages below still apply when it is.")
        else:
            note = ("Projected — credited when the client approves delivery. Change a "
                    "percentage below and this updates as you type; the platform keeps "
                    "whatever the others don't claim.")
            if not split["has_business_dev"]:
                note += (" No business developer is attributed, so the commission stays "
                         "with the platform.")
        if split["uses_override"]:
            note += " This project overrides the site defaults."

        head = (
            '<th style="text-align:left;padding:0 16px 8px 0;color:#8B93A0;font-weight:600;'
            'font-size:11px;letter-spacing:.05em;white-space:nowrap">{}</th>'
        )
        table = format_html(
            '<table data-ril-payout data-quote="{}" data-has-bd="{}" style="border-collapse:collapse;'
            'margin-bottom:8px"><tr>' + head + head + head + head + '</tr>{}</table>'
            '<div style="color:#8B93A0;font-size:12.5px;max-width:620px">{}</div>',
            split["quote_usd"], "1" if split["has_business_dev"] else "",
            "SHARE", "%",
            format_html("OF {}", _usd(split["quote_usd"])), "WHO", body, note,
        )
        if split["is_settled"]:
            return table
        return format_html("{}{}", table, self._payout_live_script())

    @staticmethod
    def _payout_live_script():
        """Recalculate the breakdown in place while the percentages are edited."""
        return mark_safe("""
<script>
(function () {
  // The percentage inputs render after this block, so wait for the full form
  // before wiring anything up.
  var ready = function (fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  };
  ready(function () {
  var table = document.querySelector('table[data-ril-payout]');
  if (!table) return;
  var expertIn = document.getElementById('id_expert_share_percent');
  var leadIn = document.getElementById('id_delivery_lead_share_percent');
  var bdIn = document.getElementById('id_business_dev_share_percent');
  var quoteIn = document.getElementById('id_quote_usd');
  // A commission only applies when a business developer is attributed; without
  // one the platform keeps it, so the row stays at zero however it's edited.
  var hasBd = table.dataset.hasBd === '1';
  if (!expertIn || !leadIn) return;

  // Falls back to the site default (the value already rendered) when a field is
  // left blank, which is exactly what the server will do on save.
  var cell = function (role, kind) {
    var row = table.querySelector('tr[data-role="' + role + '"]');
    return row && row.querySelector('td[data-cell="' + kind + '"]');
  };
  var defaults = {
    expert: parseFloat(cell('expert', 'pct').textContent),
    lead: parseFloat(cell('lead', 'pct').textContent),
    bizdev: parseFloat(cell('bizdev', 'pct').textContent)
  };
  var usd = function (n) {
    return '$' + n.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
  };
  var num = function (input, fallback) {
    var v = parseFloat(input.value);
    return isNaN(v) ? fallback : v;
  };

  var update = function () {
    var quote = quoteIn ? (parseFloat(quoteIn.value) || 0) : parseFloat(table.dataset.quote) || 0;
    var expert = num(expertIn, defaults.expert);
    var lead = num(leadIn, defaults.lead);
    var bizdev = hasBd ? num(bdIn, defaults.bizdev) : 0;
    var platform = 100 - expert - lead - bizdev;
    var over = platform < 0;
    [['expert', expert], ['lead', lead], ['bizdev', bizdev],
     ['platform', platform]].forEach(function (pair) {
      var pct = pair[1];
      cell(pair[0], 'pct').textContent = pct.toFixed(2) + '%';
      cell(pair[0], 'usd').textContent = usd(quote * pct / 100);
    });
    var row = table.querySelector('tr[data-role="platform"]');
    row.style.color = over ? '#C2410C' : '';
    cell('platform', 'pct').textContent =
      platform.toFixed(2) + '%' + (over ? ' — over 100%, this won\\'t save' : '');
  };

  [expertIn, leadIn, bdIn, quoteIn].forEach(function (el) {
    if (el) el.addEventListener('input', update);
  });
  });
})();
</script>""")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "assignee", "amount_usd", "status", "order")
    list_filter = ("status",)
    search_fields = ("title", "project__code", "project__title")
    readonly_fields = ("submitted_at", "approved_at", "approved_by", "created_at")


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("project", "kind", "author_name", "role_label", "created_at")
    list_filter = ("kind", "role_label")
    search_fields = ("text", "author_name", "project__code")
    readonly_fields = ("created_at",)


@admin.register(Engagement)
class EngagementAdmin(admin.ModelAdmin):
    list_display = ("title", "organisation", "monthly_amount_usd", "billing_day",
                    "status", "started_on", "ends_on")
    list_filter = ("status", "product_line")
    search_fields = ("title", "organisation__name", "client__email")


@admin.register(CycleRun)
class CycleRunAdmin(admin.ModelAdmin):
    """Read-only. "Did the billing job fire, and what did it do?" has to be
    answerable here rather than from a server log nobody can reach."""

    list_display = ("ran_at", "dry_run", "created_count", "skipped_count",
                    "triggered_by")
    list_filter = ("dry_run",)
    readonly_fields = ("ran_at", "dry_run", "created_count", "skipped_count",
                       "detail", "triggered_by")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
