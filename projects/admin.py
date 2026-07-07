from django.contrib import admin

from .models import Activity, Project, Task


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
    list_display = ("code", "title", "company", "category", "stage", "quote_usd", "developer", "created_at")
    list_filter = ("stage", "category")
    search_fields = ("code", "title", "company", "client__full_name", "client__email")
    autocomplete_fields = ("client", "developer")
    readonly_fields = ("code", "created_at")
    date_hierarchy = "created_at"
    inlines = [TaskInline, ActivityInline]


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
