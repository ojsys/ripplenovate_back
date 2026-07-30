from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .models import Activity, Project, Task


def _usd(amount):
    return "${:,.2f}".format(amount or 0)


def _pct(value):
    """60.00 → '60%', 12.50 → '12.5%' — compact enough for a table column."""
    return "{}%".format(("%f" % value).rstrip("0").rstrip("."))


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
    list_display = ("code", "title", "company", "category", "stage", "quote_usd",
                    "payout_split_column", "developer", "lead", "created_at")
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

    def get_queryset(self, request):
        # payout_split() reads credited earnings for delivered projects.
        return super().get_queryset(request).prefetch_related("earnings")

    @admin.display(description="Split dev/lead/platform")
    def payout_split_column(self, obj):
        """At-a-glance split, so the platform's cut is visible from the list."""
        split = obj.payout_split()
        label = "{}/{}/{}".format(
            _pct(split["developer_percent"]),
            _pct(split["delivery_lead_percent"]),
            _pct(split["platform_percent"]),
        )
        if split["uses_override"]:
            return format_html(
                '<span title="This project overrides the site defaults" '
                'style="color:#0B7D61;font-weight:600">{} ★</span>', label)
        return label

    @admin.display(description="Where the quote goes")
    def payout_breakdown(self, obj):
        """The full split — developer, delivery lead, and the platform's remainder.

        On an unsettled project the figures recalculate as you type the two
        percentages, so the platform's share is visible before you save.
        """
        if obj is None or not obj.pk:
            return "Save the project to see its payout split."
        split = obj.payout_split()

        rows = [
            ("developer", "Developer", split["developer_percent"], split["developer_usd"],
             obj.developer.full_name if obj.developer else "unassigned"),
            ("lead", "Delivery lead", split["delivery_lead_percent"], split["delivery_lead_usd"],
             obj.lead.full_name if obj.lead else "unassigned"),
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
            note = ("Projected — credited when the client approves delivery. Change either "
                    "percentage below and this updates as you type; the platform keeps "
                    "whatever the two don't claim.")
        if split["uses_override"]:
            note += " This project overrides the site defaults."

        head = (
            '<th style="text-align:left;padding:0 16px 8px 0;color:#8B93A0;font-weight:600;'
            'font-size:11px;letter-spacing:.05em;white-space:nowrap">{}</th>'
        )
        table = format_html(
            '<table data-ril-payout data-quote="{}" style="border-collapse:collapse;'
            'margin-bottom:8px"><tr>' + head + head + head + head + '</tr>{}</table>'
            '<div style="color:#8B93A0;font-size:12.5px;max-width:620px">{}</div>',
            split["quote_usd"], "SHARE", "%",
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
  var devIn = document.getElementById('id_developer_share_percent');
  var leadIn = document.getElementById('id_delivery_lead_share_percent');
  var quoteIn = document.getElementById('id_quote_usd');
  if (!devIn || !leadIn) return;

  // Falls back to the site default (the value already rendered) when a field is
  // left blank, which is exactly what the server will do on save.
  var cell = function (role, kind) {
    var row = table.querySelector('tr[data-role="' + role + '"]');
    return row && row.querySelector('td[data-cell="' + kind + '"]');
  };
  var defaults = {
    developer: parseFloat(cell('developer', 'pct').textContent),
    lead: parseFloat(cell('lead', 'pct').textContent)
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
    var dev = num(devIn, defaults.developer);
    var lead = num(leadIn, defaults.lead);
    var platform = 100 - dev - lead;
    var over = platform < 0;
    [['developer', dev], ['lead', lead], ['platform', platform]].forEach(function (pair) {
      var pct = pair[1];
      cell(pair[0], 'pct').textContent = pct.toFixed(2) + '%';
      cell(pair[0], 'usd').textContent = usd(quote * pct / 100);
    });
    var row = table.querySelector('tr[data-role="platform"]');
    row.style.color = over ? '#C2410C' : '';
    cell('platform', 'pct').textContent =
      platform.toFixed(2) + '%' + (over ? ' — over 100%, this won\\'t save' : '');
  };

  [devIn, leadIn, quoteIn].forEach(function (el) {
    if (el) el.addEventListener('input', update);
  });
  });
})();
</script>""")


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
