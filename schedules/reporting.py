from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse

from openpyxl import Workbook
from openpyxl.styles import Font

from core.access import get_accessible_schedules_queryset
from schedules.models import ScheduleBalanceMovement, ScheduleLine, WeeklySchedule
from schedules.services import build_line_day_breakdown


def build_excel_response(title: str, headers: list[str], rows: list[list[object]], filename: str) -> HttpResponse:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title[:31]
    worksheet.append(headers)

    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        worksheet.append(row)

    for column_cells in worksheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        worksheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 38)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def get_accessible_schedule_queryset_for_range(user, date_from: date, date_to: date, site=None):
    queryset = WeeklySchedule.objects.select_related("site").prefetch_related("lines")
    queryset = get_accessible_schedules_queryset(user, queryset)
    queryset = queryset.filter(week_end_date__gte=date_from, week_start_date__lte=date_to)
    if site is not None:
        queryset = queryset.filter(site=site)
    return queryset.order_by("week_start_date", "site__code")


def build_special_days_report_rows(schedules: list[WeeklySchedule], date_from: date, date_to: date) -> list[list[object]]:
    rows: list[list[object]] = []
    for schedule in schedules:
        for line in schedule.lines.all():
            for day_info in build_line_day_breakdown(line):
                if not (date_from <= day_info["date"] <= date_to):
                    continue
                if Decimal(str(day_info["worked_hours"])) <= Decimal("0.00"):
                    continue
                label = str(day_info["special_label"] or "")
                if not label:
                    continue
                rows.append(
                    [
                        day_info["date"],
                        schedule.site.name,
                        line.employee_identifier,
                        line.employee_name,
                        label,
                    ]
                )
    return rows


def build_night_bonus_report_rows(schedules: list[WeeklySchedule], date_from: date, date_to: date) -> list[list[object]]:
    rows: list[list[object]] = []
    for schedule in schedules:
        for line in schedule.lines.all():
            for day_info in build_line_day_breakdown(line):
                if not (date_from <= day_info["date"] <= date_to):
                    continue
                night_hours = Decimal(str(day_info["night_hours"]))
                if night_hours <= Decimal("0.00"):
                    continue
                rows.append(
                    [
                        day_info["date"],
                        schedule.site.name,
                        line.employee_identifier,
                        line.employee_name,
                        float(night_hours),
                    ]
                )
    return rows


def build_overtime_balance_report_rows(user, date_from: date, date_to: date, site=None) -> list[list[object]]:
    movements = (
        ScheduleBalanceMovement.objects.select_related("site", "schedule")
        .filter(
            schedule__in=get_accessible_schedules_queryset(
                user,
                WeeklySchedule.objects.filter(week_end_date__gte=date_from, week_start_date__lte=date_to),
            ),
            movement_date__gte=date_from,
            movement_date__lte=date_to,
            equivalent_hours__gt=0,
        )
        .exclude(movement_type__in=[ScheduleBalanceMovement.MovementType.PAY_DAY, ScheduleBalanceMovement.MovementType.PAY_HOURS, ScheduleBalanceMovement.MovementType.PAY_MONEY])
        .order_by("movement_date", "site__code", "employee_name")
    )
    if site is not None:
        movements = movements.filter(site=site)

    return [
        [
            movement.movement_date,
            movement.site.name,
            movement.employee_identifier,
            movement.employee_name,
            float(movement.equivalent_hours),
        ]
        for movement in movements
    ]


def get_weekly_balance_lines(user, week_start_date: date, site=None):
    queryset = WeeklySchedule.objects.select_related("site").prefetch_related("lines")
    queryset = get_accessible_schedules_queryset(user, queryset).filter(week_start_date=week_start_date)
    if site is not None:
        queryset = queryset.filter(site=site)
    schedules = list(queryset.order_by("site__code"))
    lines: list[ScheduleLine] = []
    for schedule in schedules:
        for line in schedule.lines.all():
            line.schedule_scope = schedule
            lines.append(line)
    return lines


def build_weekly_balance_report_rows(lines: list[ScheduleLine]) -> list[list[object]]:
    return [
        [
            line.schedule_scope.site.name,
            line.job_role_name,
            line.employee_identifier,
            line.employee_name,
            float(line.accrued_total_hours_balance),
            float(line.night_bonus_hours),
        ]
        for line in lines
    ]


def get_latest_visible_lines_by_employee(user) -> list[ScheduleLine]:
    queryset = (
        get_accessible_schedules_queryset(
            user,
            WeeklySchedule.objects.select_related("site").prefetch_related("lines"),
        )
        .order_by("-week_start_date", "site__code")
    )
    latest_by_employee: dict[str, ScheduleLine] = {}
    for schedule in queryset:
        for line in schedule.lines.all():
            employee_identifier = (line.employee_identifier or "").strip()
            if not employee_identifier or employee_identifier in latest_by_employee:
                continue
            latest_by_employee[employee_identifier] = line
    return list(latest_by_employee.values())


def build_balance_role_breakdown(lines: list[ScheduleLine]) -> dict[str, object]:
    days_by_role: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    hours_by_role: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    total_days = Decimal("0.00")
    total_hours = Decimal("0.00")

    for line in lines:
        role_name = (line.job_role_name or "Sin cargo").strip() or "Sin cargo"
        day_value = Decimal(str(line.accrued_day_balance or "0"))
        hour_value = Decimal(str(line.accrued_hour_balance or "0"))
        days_by_role[role_name] += day_value
        hours_by_role[role_name] += hour_value
        total_days += day_value
        total_hours += hour_value

    return {
        "total_days": total_days,
        "total_hours": total_hours,
        "days_by_role": sorted(days_by_role.items(), key=lambda item: item[0].casefold()),
        "hours_by_role": sorted(hours_by_role.items(), key=lambda item: item[0].casefold()),
    }
