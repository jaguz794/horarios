from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

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


def excel_number(value) -> int | float:
    decimal_value = Decimal(str(value or "0"))
    if decimal_value == decimal_value.to_integral():
        return int(decimal_value)
    return float(decimal_value)


def build_schedule_excel_response(schedule: WeeklySchedule) -> HttpResponse:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Horario"

    day_columns = schedule.get_day_columns()
    worksheet["A1"] = f"Horario de {schedule.site.name}"
    worksheet["A2"] = f"Semana del {schedule.week_start_date:%d/%m/%Y} al {schedule.week_end_date:%d/%m/%Y}"
    worksheet["A3"] = f"Estado: {schedule.get_status_display()}"
    worksheet["A4"] = f"Notas: {schedule.notes or 'Sin notas'}"
    total_columns = 3 + (len(day_columns) * 3) + 5
    worksheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_columns)
    worksheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=total_columns)
    worksheet.merge_cells(start_row=3, start_column=1, end_row=3, end_column=total_columns)
    worksheet.merge_cells(start_row=4, start_column=1, end_row=4, end_column=total_columns)

    for cell_ref in ("A1", "A2", "A3", "A4"):
        worksheet[cell_ref].font = Font(bold=True)
        worksheet[cell_ref].alignment = Alignment(horizontal="left")

    header_group_row = 6
    header_detail_row = 7

    worksheet.cell(row=header_group_row, column=1, value="Cedula")
    worksheet.cell(row=header_group_row, column=2, value="Empleado")
    worksheet.cell(row=header_group_row, column=3, value="Cargo")
    worksheet.merge_cells(start_row=header_group_row, start_column=1, end_row=header_detail_row, end_column=1)
    worksheet.merge_cells(start_row=header_group_row, start_column=2, end_row=header_detail_row, end_column=2)
    worksheet.merge_cells(start_row=header_group_row, start_column=3, end_row=header_detail_row, end_column=3)

    current_column = 4
    for column in day_columns:
        worksheet.cell(
            row=header_group_row,
            column=current_column,
            value=f"{column['label']}\n{column['date']}",
        )
        worksheet.merge_cells(
            start_row=header_group_row,
            start_column=current_column,
            end_row=header_group_row,
            end_column=current_column + 2,
        )
        worksheet.cell(row=header_detail_row, column=current_column, value="Turno 1")
        worksheet.cell(row=header_detail_row, column=current_column + 1, value="Turno 2")
        worksheet.cell(row=header_detail_row, column=current_column + 2, value="Horas")
        current_column += 3

    summary_headers = ["Total", "Extras", "Rec. noct.", "Dias acum.", "Horas acum."]
    for summary_header in summary_headers:
        worksheet.cell(row=header_group_row, column=current_column, value=summary_header)
        worksheet.merge_cells(
            start_row=header_group_row,
            start_column=current_column,
            end_row=header_detail_row,
            end_column=current_column,
        )
        current_column += 1

    for row_number in (header_group_row, header_detail_row):
        for cell in worksheet[row_number]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ordered_lines = schedule.lines.all().order_by("job_role_name", "employee_name", "employee_identifier")
    for line in ordered_lines:
        row = [
            line.employee_identifier,
            line.employee_name,
            line.job_role_name,
        ]
        for index in range(7):
            row.extend(
                [
                    getattr(line, f"day_{index}_shift_1", "") or "",
                    getattr(line, f"day_{index}_shift_2", "") or "",
                    excel_number(getattr(line, f"day_{index}_hours", Decimal("0.00"))),
                ]
            )
        row.extend(
            [
                excel_number(line.total_hours),
                excel_number(line.overtime_hours),
                excel_number(line.night_bonus_hours),
                excel_number(line.accrued_day_balance),
                excel_number(line.accrued_hour_balance),
            ]
        )
        worksheet.append(row)

    for column_index, column_cells in enumerate(
        worksheet.iter_cols(min_row=header_group_row, max_row=worksheet.max_row),
        start=1,
    ):
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        worksheet.column_dimensions[get_column_letter(column_index)].width = min(max(max_length + 2, 12), 26)

    worksheet.freeze_panes = "D8"
    worksheet.sheet_view.zoomScale = 85
    worksheet.page_setup.orientation = "landscape"
    worksheet.page_setup.fitToWidth = 1

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    filename = f"horario_{schedule.site.code}_{schedule.week_start_date:%Y%m%d}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def build_initial_balance_template_response() -> HttpResponse:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Plantilla"
    worksheet.append(["Cedula", "Nombre", "Dias", "Horas"])
    worksheet.append(["1000123456", "Empleado Ejemplo", 2, 4.5])

    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    for column_cells in worksheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        worksheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 24)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="plantilla_saldos_iniciales.xlsx"'
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
