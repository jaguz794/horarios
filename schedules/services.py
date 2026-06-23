from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
import re
import unicodedata

from core.models import JobRole, ShiftTemplate, SystemConfiguration
from django.db.utils import OperationalError, ProgrammingError
from legacy.services import fetch_active_staff_for_site
from openpyxl import load_workbook
from schedules.calendar_utils import get_special_day_label
from schedules.models import (
    EmployeeInitialBalance,
    EmployeeScheduleBlacklist,
    EmployeeOvertimeRestriction,
    ScheduleBalanceMovement,
    ScheduleLine,
    WeeklySchedule,
)

NON_WORKED_SHIFT_LABELS = {
    "",
    "-",
    "descanso",
    "incapacidad",
    "traslado",
    "vacaciones",
    "renuncia",
    "licencia",
}
REST_SHIFT_LABEL = "descanso"
SHIFT_PATTERN = re.compile(r"^(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2})$")
TWO_DECIMALS = Decimal("0.01")


def normalize_shift_label(value: str) -> str:
    return (value or "").strip()


def decimal_hours(value: float | Decimal | int | str) -> Decimal:
    return Decimal(str(value or "0")).quantize(TWO_DECIMALS)


def compute_night_hours(start_time, end_time, night_shift_start) -> Decimal:
    if not start_time or not end_time:
        return Decimal("0.00")

    anchor_date = date.today()
    start_value = datetime.combine(anchor_date, start_time)
    end_value = datetime.combine(anchor_date, end_time)
    if end_value <= start_value:
        end_value += timedelta(days=1)

    night_start = datetime.combine(anchor_date, night_shift_start)
    if start_value >= night_start:
        return decimal_hours((end_value - start_value).total_seconds() / 3600)
    if end_value <= night_start:
        return Decimal("0.00")
    return decimal_hours((end_value - night_start).total_seconds() / 3600)


def resolve_shift_metrics(
    label: str,
    config: SystemConfiguration | None = None,
    shift_templates: dict[str, ShiftTemplate] | None = None,
) -> tuple[Decimal, Decimal]:
    normalized = normalize_shift_label(label)
    if normalized.casefold() in NON_WORKED_SHIFT_LABELS:
        return Decimal("0.00"), Decimal("0.00")

    config = config or SystemConfiguration.load()
    template = None
    if shift_templates is not None:
        template = shift_templates.get(normalized)
    elif normalized:
        template = ShiftTemplate.objects.filter(label=normalized, is_active=True).first()

    if template:
        if not template.counts_as_worked_time:
            return Decimal("0.00"), Decimal("0.00")
        total_hours = decimal_hours(template.duration_hours)
        computed_night_hours = compute_night_hours(
            template.start_time,
            template.end_time,
            config.night_shift_start,
        )
        return total_hours, computed_night_hours

    match = SHIFT_PATTERN.match(normalized)
    if not match:
        return Decimal("0.00"), Decimal("0.00")

    start_time = datetime.strptime(match.group("start"), "%H:%M").time()
    end_time = datetime.strptime(match.group("end"), "%H:%M").time()
    start_value = datetime.combine(date.today(), start_time)
    end_value = datetime.combine(date.today(), end_time)
    if end_value <= start_value:
        end_value += timedelta(days=1)
    total_seconds = (end_value - start_value).total_seconds()
    total_hours = decimal_hours(total_seconds / 3600)
    night_hours = compute_night_hours(start_time, end_time, config.night_shift_start)
    return total_hours, night_hours


def parse_shift_hours(label: str) -> Decimal:
    total_hours, _ = resolve_shift_metrics(label)
    return total_hours


def parse_shift_night_hours(label: str) -> Decimal:
    _, night_hours = resolve_shift_metrics(label)
    return night_hours


def build_shift_metrics_catalog(config: SystemConfiguration | None = None) -> dict[str, dict[str, str | bool]]:
    config = config or SystemConfiguration.load()
    templates = {
        shift.label: shift
        for shift in ShiftTemplate.objects.filter(is_active=True)
    }
    catalog: dict[str, dict[str, str | bool]] = {}
    for label, template in templates.items():
        total_hours, night_hours = resolve_shift_metrics(label, config=config, shift_templates=templates)
        catalog[label] = {
            "hours": str(total_hours),
            "night_hours": str(night_hours),
            "counts_as_worked_time": template.counts_as_worked_time,
        }
    return catalog


def get_rest_shift_label() -> str:
    template = ShiftTemplate.objects.filter(label__iexact=REST_SHIFT_LABEL, is_active=True).first()
    return template.label if template else REST_SHIFT_LABEL


def get_line_day_reference_hours(
    line: ScheduleLine,
    config: SystemConfiguration | None = None,
) -> Decimal:
    config = config or SystemConfiguration.load()
    reference_hours = line.daily_max_hours or config.default_daily_max_hours or Decimal("0.00")
    return decimal_hours(reference_hours)


def get_active_overtime_restriction(employee_identifier: str) -> EmployeeOvertimeRestriction | None:
    cleaned_identifier = (employee_identifier or "").strip()
    if not cleaned_identifier:
        return None
    try:
        return (
            EmployeeOvertimeRestriction.objects.filter(
                employee_identifier=cleaned_identifier,
                is_active=True,
            )
            .only(
                "employee_identifier",
                "employee_name",
                "max_daily_overtime_hours",
                "max_weekly_overtime_hours",
            )
            .first()
        )
    except (ProgrammingError, OperationalError):
        return None


def get_blacklisted_employee_identifiers(
    employee_identifiers: list[str] | set[str] | tuple[str, ...] | None = None,
) -> set[str]:
    cleaned_identifiers = {
        (employee_identifier or "").strip()
        for employee_identifier in (employee_identifiers or [])
        if (employee_identifier or "").strip()
    }
    if employee_identifiers is not None and not cleaned_identifiers:
        return set()

    try:
        queryset = EmployeeScheduleBlacklist.objects.filter(is_active=True)
        if cleaned_identifiers:
            queryset = queryset.filter(employee_identifier__in=cleaned_identifiers)
        return set(queryset.values_list("employee_identifier", flat=True))
    except (ProgrammingError, OperationalError):
        return set()


def is_employee_blacklisted(employee_identifier: str) -> bool:
    cleaned_identifier = (employee_identifier or "").strip()
    if not cleaned_identifier:
        return False
    return cleaned_identifier in get_blacklisted_employee_identifiers([cleaned_identifier])


def get_daily_overtime_hours(worked_hours: Decimal, day_reference_hours: Decimal) -> Decimal:
    return max(
        decimal_hours(worked_hours) - decimal_hours(day_reference_hours),
        Decimal("0.00"),
    ).quantize(TWO_DECIMALS)


def get_selected_shift_templates(line: ScheduleLine) -> dict[str, ShiftTemplate]:
    selected_labels = {
        normalize_shift_label(getattr(line, f"day_{index}_shift_{slot}"))
        for index in range(7)
        for slot in (1, 2)
        if normalize_shift_label(getattr(line, f"day_{index}_shift_{slot}"))
    }
    return {
        shift.label: shift
        for shift in ShiftTemplate.objects.filter(label__in=selected_labels, is_active=True)
    }


def build_line_day_breakdown(
    line: ScheduleLine,
    config: SystemConfiguration | None = None,
    shift_templates: dict[str, ShiftTemplate] | None = None,
) -> list[dict[str, Decimal | date | str | int]]:
    schedule = getattr(line, "schedule", None)
    if schedule is None or not schedule.week_start_date:
        return []

    config = config or SystemConfiguration.load()
    shift_templates = shift_templates or get_selected_shift_templates(line)
    breakdown: list[dict[str, Decimal | date | str | int]] = []

    for index in range(7):
        day_date = schedule.week_start_date + timedelta(days=index)
        shift_1_label = getattr(line, f"day_{index}_shift_1", "") or ""
        shift_2_label = getattr(line, f"day_{index}_shift_2", "") or ""
        shift_1_hours, shift_1_night = resolve_shift_metrics(
            shift_1_label,
            config=config,
            shift_templates=shift_templates,
        )
        shift_2_hours, shift_2_night = resolve_shift_metrics(
            shift_2_label,
            config=config,
            shift_templates=shift_templates,
        )
        compensation_mode = getattr(line, f"day_{index}_compensation_mode", "") or ""
        compensation_hours = decimal_hours(getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0")
        breakdown.append(
            {
                "index": index,
                "date": day_date,
                "worked_hours": (shift_1_hours + shift_2_hours).quantize(TWO_DECIMALS),
                "night_hours": (shift_1_night + shift_2_night).quantize(TWO_DECIMALS),
                "special_label": get_special_day_label(day_date),
                "compensation_mode": compensation_mode,
                "compensation_hours": compensation_hours,
            }
        )

    return breakdown


def schedule_day_is_special(line: ScheduleLine, index: int) -> bool:
    schedule = getattr(line, "schedule", None)
    if schedule is None or not schedule.week_start_date:
        return False
    day_date = schedule.week_start_date + timedelta(days=index)
    return bool(get_special_day_label(day_date))


def resolve_compensation_usage(
    compensation_entries: list[dict[str, Decimal | int | str]],
    *,
    available_day_balance: Decimal,
    available_hour_balance: Decimal,
    day_reference_hours: Decimal,
    weekly_target_hours: Decimal | None = None,
) -> dict[str, Decimal | int | list[int] | dict[int, dict[str, Decimal | str | bool]]]:
    remaining_day_balance = max(decimal_hours(available_day_balance), Decimal("0.00"))
    remaining_hour_balance = max(decimal_hours(available_hour_balance), Decimal("0.00"))
    day_reference_hours = max(decimal_hours(day_reference_hours), Decimal("0.00"))
    weekly_target_hours = max(decimal_hours(weekly_target_hours or "0"), Decimal("0.00"))
    cumulative_worked_hours = Decimal("0.00")
    cumulative_overtime_hours = Decimal("0.00")

    payment_days_used = 0
    payment_days_from_day_balance = 0
    payment_days_from_hour_balance = 0
    uncovered_payment_days = 0
    payment_day_hour_equivalent = Decimal("0.00")
    payment_hours_used = Decimal("0.00")
    money_payment_hours_used = Decimal("0.00")
    invalid_pay_day_indices: list[int] = []
    invalid_pay_hours_indices: list[int] = []
    invalid_pay_money_indices: list[int] = []
    day_states: dict[int, dict[str, Decimal | str | bool]] = {}

    for entry in sorted(compensation_entries, key=lambda item: int(item.get("index", 0))):
        index = int(entry.get("index", 0))
        compensation_mode = str(entry.get("mode") or "")
        compensation_hours = decimal_hours(entry.get("hours", Decimal("0.00")) or "0")
        worked_hours = decimal_hours(entry.get("worked_hours", Decimal("0.00")) or "0")
        special_generated = bool(entry.get("special_generated"))
        day_state: dict[str, Decimal | str | bool] = {
            "mode": compensation_mode,
            "requested_hours": compensation_hours,
            "source": "",
            "valid": True,
            "available_day_balance": remaining_day_balance,
            "available_hour_balance": remaining_hour_balance,
            "remaining_day_balance": remaining_day_balance,
            "remaining_hour_balance": remaining_hour_balance,
            "generated_day": False,
            "generated_hours": Decimal("0.00"),
        }

        if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
            payment_days_used += 1
            if remaining_day_balance >= Decimal("1.00"):
                remaining_day_balance = (remaining_day_balance - Decimal("1.00")).quantize(TWO_DECIMALS)
                payment_days_from_day_balance += 1
                day_state["source"] = "day_balance"
            elif day_reference_hours > Decimal("0.00") and remaining_hour_balance >= day_reference_hours:
                remaining_hour_balance = (remaining_hour_balance - day_reference_hours).quantize(TWO_DECIMALS)
                payment_days_from_hour_balance += 1
                payment_day_hour_equivalent += day_reference_hours
                day_state["source"] = "hour_balance"
            else:
                uncovered_payment_days += 1
                invalid_pay_day_indices.append(index)
                day_state["source"] = "insufficient"
                day_state["valid"] = False
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
            payment_hours_used += compensation_hours
            remaining_before = remaining_hour_balance
            remaining_hour_balance = (remaining_hour_balance - compensation_hours).quantize(TWO_DECIMALS)
            if compensation_hours <= Decimal("0.00") or remaining_before < compensation_hours:
                invalid_pay_hours_indices.append(index)
                day_state["valid"] = False
            day_state["source"] = "hour_balance"
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_MONEY:
            money_payment_hours_used += compensation_hours
            remaining_before = remaining_hour_balance
            remaining_hour_balance = (remaining_hour_balance - compensation_hours).quantize(TWO_DECIMALS)
            if compensation_hours <= Decimal("0.00") or remaining_before < compensation_hours:
                invalid_pay_money_indices.append(index)
                day_state["valid"] = False
            day_state["source"] = "hour_balance"

        if special_generated and worked_hours > Decimal("0.00"):
            remaining_day_balance = (remaining_day_balance + Decimal("1.00")).quantize(TWO_DECIMALS)
            day_state["generated_day"] = True

        previous_overtime_hours = cumulative_overtime_hours
        cumulative_worked_hours = (cumulative_worked_hours + worked_hours).quantize(TWO_DECIMALS)
        cumulative_overtime_hours = max(cumulative_worked_hours - weekly_target_hours, Decimal("0.00")).quantize(
            TWO_DECIMALS
        )
        generated_hours = max(cumulative_overtime_hours - previous_overtime_hours, Decimal("0.00")).quantize(
            TWO_DECIMALS
        )
        if generated_hours > Decimal("0.00"):
            remaining_hour_balance = (remaining_hour_balance + generated_hours).quantize(TWO_DECIMALS)
            day_state["generated_hours"] = generated_hours

        day_state["remaining_day_balance"] = remaining_day_balance
        day_state["remaining_hour_balance"] = remaining_hour_balance
        day_states[index] = day_state

    return {
        "payment_days_used": payment_days_used,
        "payment_days_from_day_balance": payment_days_from_day_balance,
        "payment_days_from_hour_balance": payment_days_from_hour_balance,
        "uncovered_payment_days": uncovered_payment_days,
        "payment_day_hour_equivalent": payment_day_hour_equivalent.quantize(TWO_DECIMALS),
        "payment_hours_used": payment_hours_used.quantize(TWO_DECIMALS),
        "money_payment_hours_used": money_payment_hours_used.quantize(TWO_DECIMALS),
        "invalid_pay_day_indices": invalid_pay_day_indices,
        "invalid_pay_hours_indices": invalid_pay_hours_indices,
        "invalid_pay_money_indices": invalid_pay_money_indices,
        "remaining_day_balance": remaining_day_balance.quantize(TWO_DECIMALS),
        "remaining_hour_balance": remaining_hour_balance.quantize(TWO_DECIMALS),
        "day_states": day_states,
    }


def summarize_line_payments(line: ScheduleLine) -> tuple[int, Decimal, Decimal]:
    payment_days_used = 0
    payment_hours_used = Decimal("0.00")
    money_payment_hours_used = Decimal("0.00")

    for index in range(7):
        compensation_mode = getattr(line, f"day_{index}_compensation_mode", "") or ""
        compensation_hours = decimal_hours(
            getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0"
        )

        if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
            payment_days_used += 1
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
            payment_hours_used += compensation_hours
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_MONEY:
            money_payment_hours_used += compensation_hours

    return (
        payment_days_used,
        payment_hours_used.quantize(TWO_DECIMALS),
        money_payment_hours_used.quantize(TWO_DECIMALS),
    )


def get_schedule_line_balance_snapshot(
    line: ScheduleLine,
    config: SystemConfiguration | None = None,
) -> dict[str, Decimal]:
    config = config or SystemConfiguration.load()
    zero_balance = {
        "prior_day_balance": Decimal("0.00"),
        "prior_hour_balance": Decimal("0.00"),
        "prior_total_balance": Decimal("0.00"),
        "prior_day_equivalent_hours": Decimal("0.00"),
        "day_reference_hours": get_line_day_reference_hours(line, config=config),
    }

    employee_identifier = (line.employee_identifier or "").strip()
    schedule = getattr(line, "schedule", None)
    if not employee_identifier or schedule is None or not schedule.week_start_date:
        return zero_balance

    previous_line = (
        ScheduleLine.objects.filter(
            employee_identifier=employee_identifier,
            schedule__week_start_date__lt=schedule.week_start_date,
        )
        .exclude(pk=getattr(line, "pk", None))
        .select_related("schedule")
        .order_by("-schedule__week_start_date", "-pk")
        .first()
    )
    if previous_line is None:
        try:
            initial_balance = (
                EmployeeInitialBalance.objects.filter(employee_identifier=employee_identifier)
                .order_by("-updated_at", "-pk")
                .first()
            )
        except (ProgrammingError, OperationalError):
            return zero_balance
        if initial_balance is None:
            return zero_balance

        prior_day_balance = decimal_hours(initial_balance.initial_day_balance)
        prior_hour_balance = decimal_hours(initial_balance.initial_hour_balance)
        day_reference_hours = zero_balance["day_reference_hours"]
        return {
            "prior_day_balance": prior_day_balance,
            "prior_hour_balance": prior_hour_balance,
            "prior_total_balance": (
                prior_hour_balance + (prior_day_balance * day_reference_hours)
            ).quantize(TWO_DECIMALS),
            "prior_day_equivalent_hours": (prior_day_balance * day_reference_hours).quantize(TWO_DECIMALS),
            "day_reference_hours": day_reference_hours,
        }

    prior_day_balance = decimal_hours(previous_line.accrued_day_balance)
    prior_hour_balance = decimal_hours(previous_line.accrued_hour_balance)
    prior_total_balance = decimal_hours(previous_line.accrued_total_hours_balance)
    day_reference_hours = zero_balance["day_reference_hours"]

    return {
        "prior_day_balance": prior_day_balance,
        "prior_hour_balance": prior_hour_balance,
        "prior_total_balance": prior_total_balance,
        "prior_day_equivalent_hours": (prior_day_balance * day_reference_hours).quantize(TWO_DECIMALS),
        "day_reference_hours": day_reference_hours,
    }


def normalize_initial_balance_header(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_")


def parse_initial_balance_decimal(value: object, label: str, row_number: int) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return Decimal(str(value).strip().replace(",", ".")).quantize(TWO_DECIMALS)
    except (InvalidOperation, AttributeError, ValueError):
        raise ValueError(f"Fila {row_number}: el valor de {label} no es numerico.")


def decode_uploaded_csv(uploaded_file) -> str:
    raw_content = uploaded_file.read()
    try:
        uploaded_file.seek(0)
    except (AttributeError, OSError):
        pass

    if not raw_content:
        raise ValueError("El archivo CSV esta vacio.")

    if isinstance(raw_content, str):
        return raw_content

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw_content.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError("No fue posible leer el archivo CSV. Revisa la codificacion del archivo.")


def load_initial_balance_rows(uploaded_file) -> tuple[tuple[object, ...], list[tuple[object, ...]]]:
    file_name = (getattr(uploaded_file, "name", "") or "").strip().lower()
    if file_name.endswith(".csv"):
        csv_text = decode_uploaded_csv(uploaded_file)
        sample = csv_text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
        except csv.Error:
            dialect = csv.excel

        reader = csv.reader(StringIO(csv_text), dialect)
        rows = [
            tuple(row)
            for row in reader
            if any(str(cell or "").strip() for cell in row)
        ]
        if not rows:
            raise ValueError("El archivo no contiene encabezados.")
        return tuple(rows[0]), rows[1:]

    workbook = load_workbook(uploaded_file, data_only=True)
    worksheet = workbook.active
    header_row = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    data_rows = list(worksheet.iter_rows(min_row=2, values_only=True))
    return header_row, data_rows


def import_employee_initial_balances(uploaded_file, *, updated_by=None) -> dict[str, object]:
    header_row, data_rows = load_initial_balance_rows(uploaded_file)
    if not header_row:
        raise ValueError("El archivo no contiene encabezados.")

    header_index = {
        normalize_initial_balance_header(header): index
        for index, header in enumerate(header_row)
        if str(header or "").strip()
    }
    identifier_index = next(
        (
            index
            for key, index in header_index.items()
            if key in {
                "cedula",
                "documento",
                "numero_documento",
                "numero_de_documento",
                "identificacion",
                "employee_identifier",
            }
        ),
        None,
    )
    name_index = next(
        (
            index
            for key, index in header_index.items()
            if key in {
                "nombre",
                "nombres",
                "empleado",
                "employee_name",
                "nombre_completo",
                "nombres_apellidos",
                "nombres_y_apellidos",
            }
        ),
        None,
    )
    day_index = next(
        (
            index
            for key, index in header_index.items()
            if key in {
                "dias",
                "dias_iniciales",
                "dias_extra",
                "dias_extras",
                "dias_extra_semana",
                "dias_extras_semana",
                "saldo_dias",
                "saldo_inicial_dias",
                "initial_day_balance",
            }
        ),
        None,
    )
    hour_index = next(
        (
            index
            for key, index in header_index.items()
            if key in {
                "horas",
                "horas_iniciales",
                "horas_extra",
                "horas_extras",
                "horas_extra_semana",
                "horas_extras_semana",
                "saldo_horas",
                "saldo_inicial_horas",
                "initial_hour_balance",
            }
        ),
        None,
    )

    if identifier_index is None:
        raise ValueError("La plantilla debe incluir una columna de Cedula o Documento.")
    if day_index is None and hour_index is None:
        raise ValueError("La plantilla debe incluir al menos una columna de Dias u Horas.")

    created_count = 0
    updated_count = 0
    touched_identifiers: list[str] = []

    for row_number, row in enumerate(data_rows, start=2):
        identifier = str(row[identifier_index] or "").strip().upper() if identifier_index < len(row) else ""
        if not identifier:
            continue
        employee_name = str(row[name_index] or "").strip() if name_index is not None and name_index < len(row) else ""
        initial_day_balance = (
            parse_initial_balance_decimal(row[day_index], "dias", row_number)
            if day_index is not None and day_index < len(row)
            else Decimal("0.00")
        )
        initial_hour_balance = (
            parse_initial_balance_decimal(row[hour_index], "horas", row_number)
            if hour_index is not None and hour_index < len(row)
            else Decimal("0.00")
        )

        balance, created = EmployeeInitialBalance.objects.get_or_create(
            employee_identifier=identifier,
            defaults={
                "employee_name": employee_name,
                "initial_day_balance": initial_day_balance,
                "initial_hour_balance": initial_hour_balance,
                "created_by": updated_by,
                "updated_by": updated_by,
            },
        )
        if created:
            created_count += 1
        else:
            balance.employee_name = employee_name or balance.employee_name
            balance.initial_day_balance = initial_day_balance
            balance.initial_hour_balance = initial_hour_balance
            balance.updated_by = updated_by
            balance.save(
                update_fields=[
                    "employee_name",
                    "initial_day_balance",
                    "initial_hour_balance",
                    "updated_by",
                    "updated_at",
                ]
            )
            updated_count += 1

        touched_identifiers.append(identifier)

    if touched_identifiers:
        earliest_schedule = (
            ScheduleLine.objects.select_related("schedule")
            .filter(employee_identifier__in=touched_identifiers)
            .order_by("schedule__week_start_date", "pk")
            .first()
        )
        if earliest_schedule is not None:
            rebuild_balances_for_employees_from_week(
                earliest_schedule.schedule.week_start_date,
                touched_identifiers,
            )

    return {
        "created_count": created_count,
        "updated_count": updated_count,
        "processed_count": created_count + updated_count,
        "touched_identifiers": touched_identifiers,
    }


def get_schedule_line_compact_alert_summary(
    line: ScheduleLine,
    balance_snapshot: dict[str, Decimal] | None = None,
    config: SystemConfiguration | None = None,
) -> str:
    config = config or SystemConfiguration.load()
    balance_snapshot = balance_snapshot or get_schedule_line_balance_snapshot(line, config=config)
    manual_day_adjustment = decimal_hours(line.manual_day_adjustment)
    manual_hour_adjustment = decimal_hours(line.manual_hour_adjustment)
    payment_resolution = resolve_compensation_usage(
        [
            {
                "index": index,
                "mode": getattr(line, f"day_{index}_compensation_mode", "") or "",
                "hours": getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0",
                "worked_hours": getattr(line, f"day_{index}_hours", Decimal("0.00")) or "0",
                "special_generated": bool(
                    decimal_hours(getattr(line, f"day_{index}_hours", Decimal("0.00")) or "0") > Decimal("0.00")
                    and schedule_day_is_special(line, index)
                ),
            }
            for index in range(7)
        ],
        available_day_balance=balance_snapshot["prior_day_balance"] + manual_day_adjustment,
        available_hour_balance=balance_snapshot["prior_hour_balance"] + manual_hour_adjustment,
        day_reference_hours=balance_snapshot["day_reference_hours"],
        weekly_target_hours=line.weekly_target_hours or config.default_weekly_hours,
    )
    categories: list[str] = []
    daily_limit = line.daily_max_hours or config.default_daily_max_hours
    weekly_target = line.weekly_target_hours or config.default_weekly_hours
    overtime_restriction = get_active_overtime_restriction(line.employee_identifier)
    daily_restriction_limit = (
        decimal_hours(overtime_restriction.max_daily_overtime_hours)
        if overtime_restriction is not None
        else Decimal("0.00")
    )

    if any(
        decimal_hours(getattr(line, f"day_{index}_hours", Decimal("0.00")) or "0") > daily_limit
        for index in range(7)
    ):
        categories.append("limites del dia")

    if overtime_restriction and any(
        get_daily_overtime_hours(
            getattr(line, f"day_{index}_hours", Decimal("0.00")) or "0",
            balance_snapshot["day_reference_hours"],
        ) > daily_restriction_limit
        for index in range(7)
    ):
        categories.append("restriccion medica")

    if decimal_hours(line.total_hours) > weekly_target:
        categories.append("horas semanales")

    if (
        overtime_restriction
        and decimal_hours(line.overtime_hours) > decimal_hours(overtime_restriction.max_weekly_overtime_hours)
    ):
        categories.append("restriccion medica")

    if (
        payment_resolution["invalid_pay_day_indices"]
        or payment_resolution["invalid_pay_hours_indices"]
        or payment_resolution["invalid_pay_money_indices"]
    ):
        categories.append("saldo previo")

    if decimal_hours(line.accrued_day_balance) < Decimal("0.00") or decimal_hours(line.accrued_hour_balance) < Decimal(
        "0.00"
    ):
        categories.append("saldo a favor empresa")

    if not categories:
        return "Sin alertas"

    unique_categories = list(dict.fromkeys(categories))
    return f"Revisa {', '.join(unique_categories)}."


def recalculate_schedule_line(line: ScheduleLine) -> ScheduleLine:
    config = SystemConfiguration.load()
    shift_templates = get_selected_shift_templates(line)
    daily_limit = decimal_hours(line.daily_max_hours or config.default_daily_max_hours)
    weekly_target = decimal_hours(line.weekly_target_hours or config.default_weekly_hours)
    day_reference_hours = get_line_day_reference_hours(line, config=config)
    balance_snapshot = get_schedule_line_balance_snapshot(line, config=config)
    day_breakdown = build_line_day_breakdown(line, config=config, shift_templates=shift_templates)

    total_hours = Decimal("0.00")
    total_night_bonus = Decimal("0.00")
    warnings: list[str] = []
    special_days_generated = 0
    overtime_restriction = get_active_overtime_restriction(line.employee_identifier)
    daily_restriction_limit = (
        decimal_hours(overtime_restriction.max_daily_overtime_hours)
        if overtime_restriction is not None
        else Decimal("0.00")
    )
    weekly_restriction_limit = (
        decimal_hours(overtime_restriction.max_weekly_overtime_hours)
        if overtime_restriction is not None
        else Decimal("0.00")
    )

    for day_info in day_breakdown:
        index = int(day_info["index"])
        daily_hours = decimal_hours(day_info["worked_hours"])
        daily_night_bonus = decimal_hours(day_info["night_hours"])
        setattr(line, f"day_{index}_hours", daily_hours)
        total_hours += daily_hours
        total_night_bonus += daily_night_bonus

        if daily_hours > daily_limit:
            warnings.append(f"Dia {index + 1}: supera el maximo diario ({daily_hours} h).")

        if overtime_restriction:
            daily_overtime_hours = get_daily_overtime_hours(daily_hours, day_reference_hours)
            if daily_overtime_hours > daily_restriction_limit:
                warnings.append(
                    f"Dia {index + 1}: restriccion medica supera el tope diario de extras "
                    f"({daily_overtime_hours} h vs {daily_restriction_limit} h)."
                )

        if daily_hours > Decimal("0.00") and day_info["special_label"]:
            special_days_generated += 1

        compensation_mode = day_info["compensation_mode"]
        compensation_hours = decimal_hours(day_info["compensation_hours"])

        if compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
            compensated_day_hours = daily_hours + compensation_hours
            if compensation_hours <= Decimal("0.00"):
                warnings.append(f"Dia {index + 1}: pago horas requiere una cantidad mayor que cero.")
            elif daily_hours >= day_reference_hours:
                warnings.append(f"Dia {index + 1}: la jornada ya esta completa y no requiere pago horas.")
            elif compensated_day_hours > day_reference_hours:
                warnings.append(f"Dia {index + 1}: pago horas supera la jornada del dia.")
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_MONEY and compensation_hours <= Decimal("0.00"):
            warnings.append(f"Dia {index + 1}: pago en dinero requiere una cantidad mayor que cero.")

    line.total_hours = total_hours.quantize(TWO_DECIMALS)
    line.overtime_hours = max(line.total_hours - weekly_target, Decimal("0.00")).quantize(TWO_DECIMALS)
    line.night_bonus_hours = total_night_bonus.quantize(TWO_DECIMALS)
    line.special_days_generated = special_days_generated
    (
        line.payment_days_used,
        line.payment_hours_used,
        line.money_payment_hours_used,
    ) = summarize_line_payments(line)

    manual_day_adjustment = decimal_hours(line.manual_day_adjustment)
    manual_hour_adjustment = decimal_hours(line.manual_hour_adjustment)
    prior_day_balance = balance_snapshot["prior_day_balance"]
    prior_hour_balance = balance_snapshot["prior_hour_balance"]
    prior_total_balance = balance_snapshot["prior_total_balance"]
    payment_resolution = resolve_compensation_usage(
        [
            {
                "index": int(day_info["index"]),
                "mode": day_info["compensation_mode"],
                "hours": day_info["compensation_hours"],
                "worked_hours": day_info["worked_hours"],
                "special_generated": bool(
                    decimal_hours(day_info["worked_hours"]) > Decimal("0.00") and day_info["special_label"]
                ),
            }
            for day_info in day_breakdown
        ],
        available_day_balance=prior_day_balance + manual_day_adjustment,
        available_hour_balance=prior_hour_balance + manual_hour_adjustment,
        day_reference_hours=day_reference_hours,
        weekly_target_hours=weekly_target,
    )
    payment_days = Decimal(str(line.payment_days_used or 0))
    payment_days_from_hour_balance = Decimal(str(payment_resolution["payment_days_from_hour_balance"]))
    uncovered_payment_days = Decimal(str(payment_resolution["uncovered_payment_days"]))
    payment_hours = decimal_hours(payment_resolution["payment_hours_used"])
    money_payment_hours = decimal_hours(payment_resolution["money_payment_hours_used"])
    payment_day_hour_equivalent = decimal_hours(payment_resolution["payment_day_hour_equivalent"])

    line.accrued_day_balance = (
        prior_day_balance
        + Decimal(str(special_days_generated))
        + manual_day_adjustment
        - payment_days
        + payment_days_from_hour_balance
    ).quantize(TWO_DECIMALS)
    line.accrued_hour_balance = (
        prior_hour_balance
        + line.overtime_hours
        + manual_hour_adjustment
        - payment_hours
        - money_payment_hours
        - payment_day_hour_equivalent
    ).quantize(TWO_DECIMALS)
    line.accrued_total_hours_balance = (
        prior_total_balance
        + (Decimal(str(special_days_generated)) * day_reference_hours)
        + (manual_day_adjustment * day_reference_hours)
        + line.overtime_hours
        + manual_hour_adjustment
        - (payment_days * day_reference_hours)
        - payment_hours
        - money_payment_hours
    ).quantize(TWO_DECIMALS)

    if line.total_hours > weekly_target:
        warnings.append(f"Supera el objetivo semanal: {line.total_hours} h vs {weekly_target} h.")

    if (
        overtime_restriction
        and line.overtime_hours > weekly_restriction_limit
    ):
        warnings.append(
            "Restriccion medica: no puede superar "
            f"{weekly_restriction_limit} h extra en la semana."
        )

    if payment_resolution["invalid_pay_day_indices"]:
        warnings.append("Se intento aplicar pago dia sin un dia acumulado o las horas equivalentes disponibles.")

    if payment_resolution["invalid_pay_hours_indices"] or payment_resolution["invalid_pay_money_indices"]:
        warnings.append("Las horas descontadas superan el saldo acumulado disponible.")

    if line.accrued_day_balance < Decimal("0.00"):
        warnings.append("El saldo de dias queda a favor de la empresa.")

    if line.accrued_hour_balance < Decimal("0.00"):
        warnings.append("El saldo de horas queda a favor de la empresa.")

    line.validation_summary = " ".join(warnings)
    line.warnings_count = len(warnings)
    return line


def rebuild_schedule_line_movements(line: ScheduleLine) -> None:
    if not line.pk or not line.schedule_id:
        return

    config = SystemConfiguration.load()
    day_reference_hours = get_line_day_reference_hours(line, config=config)
    day_breakdown = build_line_day_breakdown(line, config=config)
    movement_date_default = line.schedule.week_end_date or line.schedule.week_start_date
    balance_snapshot = get_schedule_line_balance_snapshot(line, config=config)
    payment_resolution = resolve_compensation_usage(
        [
            {
                "index": int(day_info["index"]),
                "mode": day_info["compensation_mode"],
                "hours": day_info["compensation_hours"],
                "worked_hours": day_info["worked_hours"],
                "special_generated": bool(
                    decimal_hours(day_info["worked_hours"]) > Decimal("0.00") and day_info["special_label"]
                ),
            }
            for day_info in day_breakdown
        ],
        available_day_balance=balance_snapshot["prior_day_balance"] + decimal_hours(line.manual_day_adjustment),
        available_hour_balance=balance_snapshot["prior_hour_balance"] + decimal_hours(line.manual_hour_adjustment),
        day_reference_hours=day_reference_hours,
        weekly_target_hours=line.weekly_target_hours or config.default_weekly_hours,
    )

    movements: list[ScheduleBalanceMovement] = []

    for day_info in day_breakdown:
        day_date = day_info["date"]
        worked_hours = decimal_hours(day_info["worked_hours"])
        special_label = str(day_info["special_label"] or "")
        compensation_mode = day_info["compensation_mode"]
        compensation_hours = decimal_hours(day_info["compensation_hours"])

        if worked_hours > Decimal("0.00") and special_label:
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=ScheduleBalanceMovement.MovementType.SPECIAL_DAY,
                    quantity_days=Decimal("1.00"),
                    quantity_hours=Decimal("0.00"),
                    equivalent_hours=day_reference_hours,
                    description=f"{special_label} laborado",
                )
            )

        if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
            day_state = payment_resolution["day_states"].get(int(day_info["index"]), {})
            source = day_state.get("source")
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=ScheduleBalanceMovement.MovementType.PAY_DAY,
                    quantity_days=Decimal("-1.00") if source != "hour_balance" else Decimal("0.00"),
                    quantity_hours=(day_reference_hours * Decimal("-1.00")).quantize(TWO_DECIMALS)
                    if source == "hour_balance"
                    else Decimal("0.00"),
                    equivalent_hours=(day_reference_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    description="Pago con descanso"
                    if source != "hour_balance"
                    else "Pago con descanso (descuenta horas acumuladas)",
                )
            )
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS and compensation_hours != Decimal("0.00"):
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=ScheduleBalanceMovement.MovementType.PAY_HOURS,
                    quantity_days=Decimal("0.00"),
                    quantity_hours=(compensation_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    equivalent_hours=(compensation_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    description="Pago con horas",
                )
            )
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_MONEY and compensation_hours != Decimal("0.00"):
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=ScheduleBalanceMovement.MovementType.PAY_MONEY,
                    quantity_days=Decimal("0.00"),
                    quantity_hours=(compensation_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    equivalent_hours=(compensation_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    description="Pago en dinero",
                )
            )

    if line.overtime_hours > Decimal("0.00"):
        movements.append(
            ScheduleBalanceMovement(
                schedule=line.schedule,
                line=line,
                site=line.schedule.site,
                employee_identifier=line.employee_identifier,
                employee_name=line.employee_name,
                job_role_name=line.job_role_name,
                movement_date=movement_date_default,
                movement_type=ScheduleBalanceMovement.MovementType.OVERTIME,
                quantity_days=Decimal("0.00"),
                quantity_hours=line.overtime_hours,
                equivalent_hours=line.overtime_hours,
                description="Horas extras de la semana",
            )
        )

    manual_day_adjustment = decimal_hours(line.manual_day_adjustment)
    if manual_day_adjustment != Decimal("0.00"):
        movements.append(
            ScheduleBalanceMovement(
                schedule=line.schedule,
                line=line,
                site=line.schedule.site,
                employee_identifier=line.employee_identifier,
                employee_name=line.employee_name,
                job_role_name=line.job_role_name,
                movement_date=movement_date_default,
                movement_type=ScheduleBalanceMovement.MovementType.MANUAL_DAY,
                quantity_days=manual_day_adjustment,
                quantity_hours=Decimal("0.00"),
                equivalent_hours=(manual_day_adjustment * day_reference_hours).quantize(TWO_DECIMALS),
                description="Ajuste manual de dias",
            )
        )

    manual_hour_adjustment = decimal_hours(line.manual_hour_adjustment)
    if manual_hour_adjustment != Decimal("0.00"):
        movements.append(
            ScheduleBalanceMovement(
                schedule=line.schedule,
                line=line,
                site=line.schedule.site,
                employee_identifier=line.employee_identifier,
                employee_name=line.employee_name,
                job_role_name=line.job_role_name,
                movement_date=movement_date_default,
                movement_type=ScheduleBalanceMovement.MovementType.MANUAL_HOUR,
                quantity_days=Decimal("0.00"),
                quantity_hours=manual_hour_adjustment,
                equivalent_hours=manual_hour_adjustment,
                description="Ajuste manual de horas",
            )
        )

    line.balance_movements.all().delete()
    if movements:
        ScheduleBalanceMovement.objects.bulk_create(movements)


def save_schedule_line_with_balances(line: ScheduleLine) -> ScheduleLine:
    recalculate_schedule_line(line)
    line.save()
    rebuild_schedule_line_movements(line)
    return line


def rebuild_balances_for_employees_from_week(
    week_start_date: date,
    employee_identifiers: list[str] | set[str] | tuple[str, ...] | None = None,
) -> None:
    queryset = ScheduleLine.objects.select_related("schedule").filter(
        schedule__week_start_date__gte=week_start_date,
    )
    if employee_identifiers:
        queryset = queryset.filter(employee_identifier__in=set(employee_identifiers))

    for line in queryset.order_by("schedule__week_start_date", "schedule__site__code", "pk"):
        save_schedule_line_with_balances(line)


def purge_blacklisted_lines_from_schedule(schedule: WeeklySchedule) -> list[str]:
    blacklisted_identifiers = get_blacklisted_employee_identifiers(
        schedule.lines.values_list("employee_identifier", flat=True)
    )
    if not blacklisted_identifiers:
        return []

    schedule.lines.filter(employee_identifier__in=blacklisted_identifiers).delete()
    rebuild_balances_for_employees_from_week(schedule.week_start_date, list(blacklisted_identifiers))
    return sorted(blacklisted_identifiers)


def copy_schedule_template(source_schedule: WeeklySchedule, target_schedule: WeeklySchedule) -> tuple[int, int]:
    if source_schedule.pk == target_schedule.pk:
        return 0, 0

    blacklisted_identifiers = get_blacklisted_employee_identifiers(
        source_schedule.lines.values_list("employee_identifier", flat=True)
    )
    source_lines = {
        (line.employee_identifier or "").strip(): line
        for line in source_schedule.lines.all()
        if (line.employee_identifier or "").strip() not in blacklisted_identifiers
    }
    copied_count = 0
    touched_employee_identifiers: list[str] = []

    for line in target_schedule.lines.all():
        employee_identifier = (line.employee_identifier or "").strip()
        if employee_identifier in blacklisted_identifiers:
            continue
        source_line = source_lines.get(employee_identifier)
        if source_line is None:
            continue

        for index in range(7):
            setattr(line, f"day_{index}_shift_1", getattr(source_line, f"day_{index}_shift_1", "") or "")
            setattr(line, f"day_{index}_shift_2", getattr(source_line, f"day_{index}_shift_2", "") or "")
            setattr(line, f"day_{index}_compensation_mode", "")
            setattr(line, f"day_{index}_compensation_hours", Decimal("0.00"))

        save_schedule_line_with_balances(line)
        copied_count += 1
        touched_employee_identifiers.append(employee_identifier)

    if touched_employee_identifiers:
        rebuild_balances_for_employees_from_week(target_schedule.week_start_date, touched_employee_identifiers)

    return copied_count, len(source_lines)


def sync_schedule_from_legacy(schedule: WeeklySchedule) -> tuple[int, int]:
    config = SystemConfiguration.load()
    purge_blacklisted_lines_from_schedule(schedule)
    staff = fetch_active_staff_for_site(
        schedule.site.code,
        week_start_date=schedule.week_start_date,
    )
    blacklisted_identifiers = get_blacklisted_employee_identifiers(
        [employee.employee_id for employee in staff]
    )
    existing_lines = {
        line.employee_identifier: line for line in schedule.lines.all()
    }
    created_count = 0
    updated_count = 0
    touched_employee_identifiers: list[str] = []

    for employee in staff:
        if employee.employee_id in blacklisted_identifiers:
            continue
        job_role, _ = JobRole.objects.get_or_create(
            name=employee.role_name or "SIN CARGO",
            defaults={
                "code": employee.role_code,
                "weekly_target_hours": config.default_weekly_hours,
                "daily_max_hours": config.default_daily_max_hours,
            },
        )

        line = existing_lines.get(employee.employee_id)
        if line is None:
            line = ScheduleLine(
                schedule=schedule,
                employee_identifier=employee.employee_id,
            )
            created_count += 1
        else:
            updated_count += 1

        line.employee_name = employee.employee_name
        line.department_code = employee.department_code
        line.department_name = employee.department_name
        line.job_role_code = employee.role_code
        line.job_role_name = employee.role_name
        line.weekly_target_hours = job_role.weekly_target_hours
        line.daily_max_hours = job_role.daily_max_hours
        save_schedule_line_with_balances(line)
        touched_employee_identifiers.append(line.employee_identifier)

    if touched_employee_identifiers:
        rebuild_balances_for_employees_from_week(schedule.week_start_date, touched_employee_identifiers)

    return created_count, updated_count
