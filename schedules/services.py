from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import re

from core.models import JobRole, ShiftTemplate, SystemConfiguration
from legacy.services import fetch_active_staff_for_site
from schedules.models import ScheduleLine, WeeklySchedule

NON_WORKED_SHIFT_LABELS = {
    "",
    "-",
    "descanso",
    "incapacidad",
    "traslado",
    "vacaciones",
}
REST_SHIFT_LABEL = "descanso"
SHIFT_PATTERN = re.compile(r"^(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2})$")
TWO_DECIMALS = Decimal("0.01")


def normalize_shift_label(value: str) -> str:
    return (value or "").strip()


def decimal_hours(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(TWO_DECIMALS)


def compute_night_hours(
    start_time,
    end_time,
    night_shift_start,
) -> Decimal:
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
        configured_night_hours = decimal_hours(template.night_bonus_hours)
        computed_night_hours = compute_night_hours(
            template.start_time,
            template.end_time,
            config.night_shift_start,
        )
        return total_hours, max(configured_night_hours, computed_night_hours)

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


def summarize_line_payments(line: ScheduleLine) -> tuple[int, Decimal]:
    payment_days_used = 0
    payment_hours_used = Decimal("0.00")

    for index in range(7):
        compensation_mode = getattr(line, f"day_{index}_compensation_mode", "") or ""
        compensation_hours = Decimal(str(getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0"))

        if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
            payment_days_used += 1
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
            payment_hours_used += compensation_hours

    return payment_days_used, payment_hours_used.quantize(TWO_DECIMALS)


def get_schedule_line_balance_snapshot(
    line: ScheduleLine,
    config: SystemConfiguration | None = None,
) -> dict[str, Decimal]:
    config = config or SystemConfiguration.load()
    zero_balance = {
        "prior_day_balance": Decimal("0.00"),
        "prior_hour_balance": Decimal("0.00"),
        "prior_total_balance": Decimal("0.00"),
        "day_reference_hours": get_line_day_reference_hours(line, config=config),
    }

    employee_identifier = (line.employee_identifier or "").strip()
    schedule = getattr(line, "schedule", None)
    if not employee_identifier or schedule is None or not schedule.week_start_date:
        return zero_balance

    prior_lines = (
        ScheduleLine.objects.filter(
            employee_identifier=employee_identifier,
            schedule__week_start_date__lt=schedule.week_start_date,
        )
        .select_related("schedule")
        .order_by("schedule__week_start_date", "pk")
    )

    prior_day_balance = Decimal("0.00")
    prior_hour_balance = Decimal("0.00")
    prior_total_balance = Decimal("0.00")

    for previous_line in prior_lines:
        previous_day_reference = get_line_day_reference_hours(previous_line, config=config)
        previous_payment_days = Decimal(str(previous_line.payment_days_used or 0))
        previous_payment_hours = Decimal(str(previous_line.payment_hours_used or "0"))
        previous_pending_days = Decimal(str(previous_line.pending_days or "0"))
        previous_pending_hours = Decimal(str(previous_line.pending_hours or "0"))
        previous_overtime_hours = Decimal(str(previous_line.overtime_hours or "0"))

        prior_day_balance += previous_pending_days - previous_payment_days
        prior_hour_balance += previous_pending_hours + previous_overtime_hours - previous_payment_hours
        prior_total_balance += (
            (previous_pending_days * previous_day_reference)
            + previous_pending_hours
            + previous_overtime_hours
            - (previous_payment_days * previous_day_reference)
            - previous_payment_hours
        )

    return {
        "prior_day_balance": prior_day_balance.quantize(TWO_DECIMALS),
        "prior_hour_balance": prior_hour_balance.quantize(TWO_DECIMALS),
        "prior_total_balance": prior_total_balance.quantize(TWO_DECIMALS),
        "prior_day_equivalent_hours": (prior_day_balance * zero_balance["day_reference_hours"]).quantize(TWO_DECIMALS),
        "day_reference_hours": zero_balance["day_reference_hours"],
    }


def get_schedule_line_compact_alert_summary(
    line: ScheduleLine,
    balance_snapshot: dict[str, Decimal] | None = None,
    config: SystemConfiguration | None = None,
) -> str:
    config = config or SystemConfiguration.load()
    balance_snapshot = balance_snapshot or get_schedule_line_balance_snapshot(line, config=config)
    categories: list[str] = []
    daily_limit = line.daily_max_hours or config.default_daily_max_hours
    weekly_target = line.weekly_target_hours or config.default_weekly_hours

    if any(
        Decimal(str(getattr(line, f"day_{index}_hours", Decimal("0.00")) or "0")) > daily_limit
        for index in range(7)
    ):
        categories.append("limites del dia")

    if Decimal(str(line.total_hours or "0")) > weekly_target:
        categories.append("horas semanales")

    if Decimal(str(len(line.pending_dates))) != Decimal(str(line.pending_days or "0")):
        categories.append("pendientes")

    if (
        Decimal(str(line.payment_days_used or 0)) > balance_snapshot["prior_day_balance"]
        or Decimal(str(line.payment_hours_used or "0")) > balance_snapshot["prior_hour_balance"]
        or Decimal(str(line.pending_hours_variance or "0")) < Decimal("0.00")
    ):
        categories.append("saldo")

    if not categories:
        return "Sin alertas"

    unique_categories = list(dict.fromkeys(categories))
    return f"Revisa {', '.join(unique_categories)}."


def recalculate_schedule_line(line: ScheduleLine) -> ScheduleLine:
    config = SystemConfiguration.load()
    total = Decimal("0.00")
    total_night_bonus = Decimal("0.00")
    warnings: list[str] = []
    daily_limit = line.daily_max_hours or config.default_daily_max_hours
    weekly_target = line.weekly_target_hours or config.default_weekly_hours
    day_reference_hours = get_line_day_reference_hours(line, config=config)
    selected_labels = {
        normalize_shift_label(getattr(line, f"day_{index}_shift_{slot}"))
        for index in range(7)
        for slot in (1, 2)
        if normalize_shift_label(getattr(line, f"day_{index}_shift_{slot}"))
    }
    shift_templates = {
        shift.label: shift
        for shift in ShiftTemplate.objects.filter(label__in=selected_labels, is_active=True)
    }

    for index in range(7):
        shift_1 = getattr(line, f"day_{index}_shift_1")
        shift_2 = getattr(line, f"day_{index}_shift_2")
        compensation_mode = getattr(line, f"day_{index}_compensation_mode", "") or ""
        compensation_hours = Decimal(str(getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0"))
        shift_1_hours, shift_1_night = resolve_shift_metrics(shift_1, config=config, shift_templates=shift_templates)
        shift_2_hours, shift_2_night = resolve_shift_metrics(shift_2, config=config, shift_templates=shift_templates)
        daily_hours = shift_1_hours + shift_2_hours
        daily_night_bonus = shift_1_night + shift_2_night
        setattr(line, f"day_{index}_hours", daily_hours)
        total += daily_hours
        total_night_bonus += daily_night_bonus
        if daily_hours > daily_limit:
            warnings.append(f"Dia {index + 1}: supera el maximo diario ({daily_hours}h).")
        if compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
            compensated_day_hours = daily_hours + compensation_hours
            if compensated_day_hours > day_reference_hours:
                warnings.append(f"Dia {index + 1}: pago horas supera la jornada del dia.")
            elif compensated_day_hours < day_reference_hours and compensation_hours > Decimal("0.00"):
                warnings.append(
                    f"Dia {index + 1}: con horas pagas aun queda incompleta la jornada ({compensated_day_hours}h de {day_reference_hours}h)."
                )

    line.total_hours = total.quantize(TWO_DECIMALS)
    line.overtime_hours = max(line.total_hours - weekly_target, Decimal("0.00")).quantize(TWO_DECIMALS)
    line.night_bonus_hours = total_night_bonus.quantize(TWO_DECIMALS)
    line.payment_days_used, line.payment_hours_used = summarize_line_payments(line)
    balance_snapshot = get_schedule_line_balance_snapshot(line, config=config)
    day_reference_hours = balance_snapshot["day_reference_hours"]
    pending_days = Decimal(str(line.pending_days or "0"))
    pending_hours = Decimal(str(line.pending_hours or "0"))
    payment_days = Decimal(str(line.payment_days_used or 0))
    payment_hours = Decimal(str(line.payment_hours_used or "0"))
    current_total_balance = (
        balance_snapshot["prior_total_balance"]
        + (pending_days * day_reference_hours)
        + pending_hours
        + line.overtime_hours
        - (payment_days * day_reference_hours)
        - payment_hours
    )
    line.pending_hours_variance = current_total_balance.quantize(TWO_DECIMALS)

    if line.total_hours > weekly_target:
        warnings.append(f"Supera el objetivo semanal: {line.total_hours}h vs {weekly_target}h.")
    pending_dates_count = len(line.pending_dates)
    if Decimal(str(pending_dates_count)) != pending_days:
        warnings.append("La cantidad de fechas pendientes debe coincidir con dias pendientes.")
    if Decimal(str(line.payment_days_used or 0)) > balance_snapshot["prior_day_balance"]:
        warnings.append("Se intento aplicar pago dia sin saldo previo suficiente.")
    if payment_hours > balance_snapshot["prior_hour_balance"]:
        warnings.append("Las horas marcadas como pago superan el saldo previo disponible.")
    if line.pending_hours_variance < Decimal("0.00"):
        warnings.append("El saldo acumulado queda negativo: se aplico mas pago del que habia acumulado.")

    for index in range(7):
        compensation_mode = getattr(line, f"day_{index}_compensation_mode", "") or ""
        compensation_hours = Decimal(str(getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0"))
        day_hours = Decimal(str(getattr(line, f"day_{index}_hours", Decimal("0.00")) or "0"))

        if compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS and compensation_hours <= Decimal("0.00"):
            warnings.append(f"Dia {index + 1}: pago horas requiere una cantidad mayor que cero.")

    line.validation_summary = " ".join(warnings)
    line.warnings_count = len(warnings)
    return line


def sync_schedule_from_legacy(schedule: WeeklySchedule) -> tuple[int, int]:
    config = SystemConfiguration.load()
    staff = fetch_active_staff_for_site(
        schedule.site.code,
        week_start_date=schedule.week_start_date,
    )
    existing_lines = {
        line.employee_identifier: line for line in schedule.lines.all()
    }
    created_count = 0
    updated_count = 0

    for employee in staff:
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
        recalculate_schedule_line(line)
        line.save()

    return created_count, updated_count
