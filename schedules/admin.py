from django.contrib import admin

from schedules.models import (
    EmployeeInitialBalance,
    EmployeeOvertimeRestriction,
    ScheduleBalanceMovement,
    ScheduleLine,
    ScheduleSettlementDocument,
    WeeklySchedule,
)


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
        "special_days_generated",
        "accrued_day_balance",
        "accrued_hour_balance",
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
        "special_days_generated",
        "accrued_day_balance",
        "accrued_hour_balance",
        "warnings_count",
    )
    list_filter = ("schedule__site",)
    search_fields = ("employee_identifier", "employee_name", "job_role_name", "department_name")


@admin.register(ScheduleBalanceMovement)
class ScheduleBalanceMovementAdmin(admin.ModelAdmin):
    list_display = (
        "movement_date",
        "site",
        "employee_identifier",
        "employee_name",
        "movement_type",
        "quantity_days",
        "quantity_hours",
        "equivalent_hours",
    )
    list_filter = ("site", "movement_type")
    search_fields = ("employee_identifier", "employee_name", "job_role_name", "description")


@admin.register(ScheduleSettlementDocument)
class ScheduleSettlementDocumentAdmin(admin.ModelAdmin):
    list_display = ("schedule", "file_name", "generated_by", "updated_at")
    list_filter = ("schedule__site",)
    search_fields = ("schedule__site__name", "schedule__site__code", "file_name")


@admin.register(EmployeeInitialBalance)
class EmployeeInitialBalanceAdmin(admin.ModelAdmin):
    list_display = ("employee_identifier", "employee_name", "initial_day_balance", "initial_hour_balance", "updated_at")
    search_fields = ("employee_identifier", "employee_name")


@admin.register(EmployeeOvertimeRestriction)
class EmployeeOvertimeRestrictionAdmin(admin.ModelAdmin):
    list_display = (
        "employee_identifier",
        "employee_name",
        "max_daily_overtime_hours",
        "max_weekly_overtime_hours",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active",)
    search_fields = ("employee_identifier", "employee_name", "notes")
