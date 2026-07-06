from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
from pathlib import Path

from django.conf import settings

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from schedules.reporting import InventoryReportEntry, SPANISH_MONTH_NAMES, build_inventory_report_entries


def group_inventory_entries(
    entries: list[InventoryReportEntry],
) -> OrderedDict[tuple[str, str, object], list[InventoryReportEntry]]:
    grouped: OrderedDict[tuple[str, str, object], list[InventoryReportEntry]] = OrderedDict()
    for entry in entries:
        key = (entry.site_code, entry.site_name, entry.inventory_date)
        grouped.setdefault(key, []).append(entry)
    return grouped


def get_inventory_report_filename(week_start_date) -> str:
    return f"planilla_inventario_{week_start_date:%Y%m%d}.pdf"


def build_inventory_week_pdf_bytes(lines, week_start_date) -> bytes:
    entries = build_inventory_report_entries(lines)
    grouped_entries = group_inventory_entries(entries)

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "InventoryTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=18,
        alignment=1,
        textColor=colors.HexColor("#173019"),
        spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "InventorySubtitle",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        alignment=1,
        textColor=colors.HexColor("#26432b"),
        spaceAfter=2,
    )
    header_label_style = ParagraphStyle(
        "InventoryHeaderLabel",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=10,
        textColor=colors.HexColor("#173019"),
        alignment=1,
    )
    header_value_style = ParagraphStyle(
        "InventoryHeaderValue",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=10,
        textColor=colors.black,
        alignment=1,
    )
    table_cell_style = ParagraphStyle(
        "InventoryCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=9.5,
        textColor=colors.black,
    )
    table_cell_center_style = ParagraphStyle(
        "InventoryCellCenter",
        parent=table_cell_style,
        alignment=1,
    )
    table_cell_bold_style = ParagraphStyle(
        "InventoryCellBold",
        parent=table_cell_style,
        fontName="Helvetica-Bold",
    )

    story = []
    logo_path = Path(settings.BASE_DIR) / "static" / "img" / "logo-popular.png"

    if not grouped_entries:
        grouped_entries = OrderedDict(
            [
                (
                    ("", "Sin sede", week_start_date),
                    [],
                )
            ]
        )

    for group_index, ((_, site_name, inventory_date), group_rows) in enumerate(grouped_entries.items()):
        if group_index > 0:
            story.append(PageBreak())

        if logo_path.exists():
            story.append(Image(str(logo_path), width=42 * mm, height=16 * mm))
            story.append(Spacer(1, 2 * mm))

        story.append(Paragraph("SUPERMERCADO POPULAR", title_style))
        story.append(Paragraph("PLANILLA INVENTARIO", subtitle_style))
        story.append(Spacer(1, 3 * mm))

        header_table = Table(
            [
                [
                    Paragraph("SEDE", header_label_style),
                    Paragraph("INVENTARIO", header_label_style),
                    Paragraph("MES", header_label_style),
                    Paragraph("FECHA INV.", header_label_style),
                ],
                [
                    Paragraph(site_name, header_value_style),
                    Paragraph("PROGRAMADO", header_value_style),
                    Paragraph(SPANISH_MONTH_NAMES[inventory_date.month].upper(), header_value_style),
                    Paragraph(inventory_date.strftime("%d/%m/%Y"), header_value_style),
                ],
            ],
            colWidths=[46 * mm, 46 * mm, 38 * mm, 38 * mm],
        )
        header_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#fff3a6")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cddc99")),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#a6c45c")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(header_table)
        story.append(Spacer(1, 4 * mm))

        table_rows = [
            [
                Paragraph("N.°", table_cell_bold_style),
                Paragraph("CEDULA N.°", table_cell_bold_style),
                Paragraph("NOMBRE Y APELLIDO", table_cell_bold_style),
                Paragraph("CARGO", table_cell_bold_style),
                Paragraph("VALOR", table_cell_bold_style),
                Paragraph("FIRMA TRAB.", table_cell_bold_style),
                Paragraph("FIRMA JEFE", table_cell_bold_style),
            ]
        ]

        if not group_rows:
            table_rows.append(
                [
                    "",
                    "",
                    Paragraph("Sin personal marcado para inventario en esta fecha.", table_cell_style),
                    "",
                    "",
                    "",
                    "",
                ]
            )
        else:
            for row_index, entry in enumerate(group_rows, start=1):
                table_rows.append(
                    [
                        Paragraph(str(row_index), table_cell_center_style),
                        Paragraph(entry.employee_identifier, table_cell_style),
                        Paragraph(entry.employee_name, table_cell_style),
                        Paragraph(entry.job_role_name, table_cell_style),
                        "",
                        "",
                        "",
                    ]
                )

        detail_table = Table(
            table_rows,
            repeatRows=1,
            colWidths=[10 * mm, 28 * mm, 56 * mm, 34 * mm, 18 * mm, 24 * mm, 24 * mm],
        )
        detail_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef5c3")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#173019")),
                    ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#cddc99")),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#a6c45c")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbfff0")]),
                ]
            )
        )
        story.append(detail_table)

    document.build(story)
    return buffer.getvalue()
