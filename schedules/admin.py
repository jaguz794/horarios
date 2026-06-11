from django.contrib import admin

from schedules.models import ScheduleLine, WeeklySchedule


class ScheduleLineInline(admin.TabularInline):
    model = ScheduleLine
    extra = 0
    fields = (
        "employee_document_type",
        "employee_identifier",
        "employee_name",
        "department_name",
        "job_role_name",
        "total_hours",
        "overtime_hours",
        "night_bonus_hours",
        "pending_hours",
        "pending_hours_variance",
        "warnings_count",
    )
    readonly_fields = fields
    show_change_link = False


@admin.register(WeeklySchedule)
class WeeklyScheduleAdmin(admin.ModelAdmin):
    list_display = ("site", "week_start_date", "week_end_date", "status", "created_by")
    list_filter = ("status", "site")
    search_fields = ("site__name", "site__code")
    inlines = [ScheduleLineInline]


@admin.register(ScheduleLine)
class ScheduleLineAdmin(admin.ModelAdmin):
    list_display = (
        "employee_document_type",
        "employee_identifier",
        "employee_name",
        "schedule",
        "department_name",
        "job_role_name",
        "total_hours",
        "overtime_hours",
        "night_bonus_hours",
        "pending_hours_variance",
        "warnings_count",
    )
    list_filter = ("schedule__site",)
    search_fields = ("employee_identifier", "employee_name", "job_role_name", "department_name")
