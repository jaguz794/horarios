from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from schedules.models import ScheduleLine, ScheduleSettlementDocument, WeeklySchedule
from schedules.services import rebuild_balances_for_employees_from_week

TWO_DECIMALS = Decimal("0.01")


@dataclass(slots=True)
class SettlementRow:
    employee_identifier: str
    employee_name: str
    job_role_name: str
    accrued_days: Decimal
    accrued_hours: Decimal


def format_balance(value: Decimal | int | float | str) -> str:
    decimal_value = Decimal(str(value or "0")).quantize(TWO_DECIMALS)
    if decimal_value == decimal_value.to_integral():
        return str(int(decimal_value))
    return format(decimal_value.normalize(), "f").rstrip("0").rstrip(".")


def get_settlement_rows(schedule: WeeklySchedule) -> list[SettlementRow]:
    rows: list[SettlementRow] = []
    for line in schedule.lines.all().order_by("job_role_name", "employee_name"):
        accrued_days = Decimal(str(line.accrued_day_balance or "0")).quantize(TWO_DECIMALS)
        accrued_hours = Decimal(str(line.accrued_hour_balance or "0")).quantize(TWO_DECIMALS)
        if accrued_days == Decimal("0.00") and accrued_hours == Decimal("0.00"):
            continue
        rows.append(
            SettlementRow(
                employee_identifier=(line.employee_identifier or "").strip(),
                employee_name=(line.employee_name or "").strip(),
                job_role_name=(line.job_role_name or "SIN CARGO").strip() or "SIN CARGO",
                accrued_days=accrued_days,
                accrued_hours=accrued_hours,
            )
        )
    return rows


def group_settlement_rows(rows: list[SettlementRow]) -> OrderedDict[str, list[SettlementRow]]:
    grouped: OrderedDict[str, list[SettlementRow]] = OrderedDict()
    for row in rows:
        grouped.setdefault(row.job_role_name, []).append(row)
    return grouped


def build_settlement_pdf_bytes(
    *,
    site_name: str,
    week_start_date,
    week_end_date,
    generated_at,
    grouped_rows: OrderedDict[str, list[SettlementRow]],
) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=12 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SettlementTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        alignment=1,
        textColor=colors.HexColor("#173019"),
        spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "SettlementSubtitle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=12,
        alignment=1,
        textColor=colors.HexColor("#26432b"),
        spaceAfter=2,
    )
    table_cell = ParagraphStyle(
        "SettlementCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.black,
    )
    table_cell_bold = ParagraphStyle(
        "SettlementCellBold",
        parent=table_cell,
        fontName="Helvetica-Bold",
    )

    story = []
    logo_path = Path(settings.BASE_DIR) / "static" / "img" / "logo-popular.png"
    if logo_path.exists():
        story.append(Image(str(logo_path), width=46 * mm, height=18 * mm))
        story.append(Spacer(1, 3 * mm))

    story.append(
        Paragraph(
            f'PAZ Y SALVO SEMANA DEL {week_start_date.strftime("%d/%m/%Y")} AL {week_end_date.strftime("%d/%m/%Y")}',
            title_style,
        )
    )
    story.append(Paragraph(f'PARA LA SEDE "{site_name}"', subtitle_style))
    story.append(Paragraph(f'FECHA {generated_at.strftime("%d/%m/%Y %H:%M")}', subtitle_style))
    story.append(Spacer(1, 4 * mm))

    table_rows = [
        [
            Paragraph("CEDULA", table_cell_bold),
            Paragraph("EMPLEADO", table_cell_bold),
            Paragraph("CARGO", table_cell_bold),
            Paragraph("DIAS ACUMULADOS", table_cell_bold),
            Paragraph("HORAS ACUMULADAS", table_cell_bold),
            Paragraph("FIRMA", table_cell_bold),
        ]
    ]

    grand_days = Decimal("0.00")
    grand_hours = Decimal("0.00")

    if not grouped_rows:
        table_rows.append(
            [
                "",
                Paragraph("Sin saldos acumulados por compensar para esta semana.", table_cell),
                "",
                "0",
                "0",
                "",
            ]
        )
    else:
        for role_name, role_rows in grouped_rows.items():
            role_days = Decimal("0.00")
            role_hours = Decimal("0.00")
            for role_row in role_rows:
                role_days += role_row.accrued_days
                role_hours += role_row.accrued_hours
                table_rows.append(
                    [
                        Paragraph(role_row.employee_identifier, table_cell),
                        Paragraph(role_row.employee_name, table_cell),
                        Paragraph(role_row.job_role_name, table_cell),
                        Paragraph(format_balance(role_row.accrued_days), table_cell),
                        Paragraph(format_balance(role_row.accrued_hours), table_cell),
                        "",
                    ]
                )
            grand_days += role_days
            grand_hours += role_hours
            table_rows.append(
                [
                    "",
                    Paragraph(f"TOTAL {role_name}", table_cell_bold),
                    "",
                    Paragraph(format_balance(role_days), table_cell_bold),
                    Paragraph(format_balance(role_hours), table_cell_bold),
                    "",
                ]
            )

        table_rows.append(
            [
                "",
                Paragraph("TOTAL GENERAL", table_cell_bold),
                "",
                Paragraph(format_balance(grand_days), table_cell_bold),
                Paragraph(format_balance(grand_hours), table_cell_bold),
                "",
            ]
        )

    table = Table(
        table_rows,
        repeatRows=1,
        colWidths=[34 * mm, 88 * mm, 48 * mm, 27 * mm, 32 * mm, 34 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#fff3a6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#173019")),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (3, 1), (4, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cedca4")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#a6c45c")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbfff0")]),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table)

    document.build(story)
    return buffer.getvalue()


def build_schedule_settlement_pdf_bytes(schedule: WeeklySchedule) -> bytes:
    grouped_rows = group_settlement_rows(get_settlement_rows(schedule))
    generated_at = timezone.localtime()
    return build_settlement_pdf_bytes(
        site_name=schedule.site.name,
        week_start_date=schedule.week_start_date,
        week_end_date=schedule.week_end_date,
        generated_at=generated_at,
        grouped_rows=grouped_rows,
    )


def get_schedule_settlement_filename(schedule: WeeklySchedule) -> str:
    site_code = (schedule.site.code or "sede").strip()
    return f"paz_y_salvo_{site_code}_{schedule.week_start_date:%Y%m%d}.pdf"


def generate_and_store_schedule_settlement(schedule: WeeklySchedule, generated_by=None) -> ScheduleSettlementDocument:
    employee_identifiers = list(schedule.lines.values_list("employee_identifier", flat=True))
    if employee_identifiers:
        rebuild_balances_for_employees_from_week(schedule.week_start_date, employee_identifiers)
        schedule.refresh_from_db()

    pdf_bytes = build_schedule_settlement_pdf_bytes(schedule)
    file_name = get_schedule_settlement_filename(schedule)
    document, _ = ScheduleSettlementDocument.objects.update_or_create(
        schedule=schedule,
        defaults={
            "file_name": file_name,
            "pdf_content": pdf_bytes,
            "generated_by": generated_by,
        },
    )

    output_dir = Path(settings.BASE_DIR) / "output" / "pdf"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / file_name).write_bytes(pdf_bytes)

    return document
