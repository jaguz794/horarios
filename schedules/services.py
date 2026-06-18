from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import re

from core.models import JobRole, ShiftTemplate, SystemConfiguration
from legacy.services import fetch_active_staff_for_site
from schedules.calendar_utils import get_special_day_label
from schedules.models import ScheduleBalanceMovement, ScheduleLine, WeeklySchedule

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
        return zero_balance

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
        decimal_hours(getattr(line, f"day_{index}_hours", Decimal("0.00")) or "0") > daily_limit
        for index in range(7)
    ):
        categories.append("limites del dia")

    if decimal_hours(line.total_hours) > weekly_target:
        categories.append("horas semanales")

    if (
        Decimal(str(line.payment_days_used or 0)) > max(balance_snapshot["prior_day_balance"], Decimal("0.00"))
        or decimal_hours(line.payment_hours_used) + decimal_hours(line.money_payment_hours_used)
        > max(balance_snapshot["prior_hour_balance"], Decimal("0.00"))
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

    for day_info in day_breakdown:
        index = int(day_info["index"])
        daily_hours = decimal_hours(day_info["worked_hours"])
        daily_night_bonus = decimal_hours(day_info["night_hours"])
        setattr(line, f"day_{index}_hours", daily_hours)
        total_hours += daily_hours
        total_night_bonus += daily_night_bonus

        if daily_hours > daily_limit:
            warnings.append(f"Dia {index + 1}: supera el maximo diario ({daily_hours} h).")

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
    if manual_day_adjustment == Decimal("0.00") and decimal_hours(line.pending_days) != Decimal("0.00"):
        manual_day_adjustment = decimal_hours(line.pending_days)
        line.manual_day_adjustment = manual_day_adjustment
    if manual_hour_adjustment == Decimal("0.00") and decimal_hours(line.pending_hours) != Decimal("0.00"):
        manual_hour_adjustment = decimal_hours(line.pending_hours)
        line.manual_hour_adjustment = manual_hour_adjustment
    prior_day_balance = balance_snapshot["prior_day_balance"]
    prior_hour_balance = balance_snapshot["prior_hour_balance"]
    prior_total_balance = balance_snapshot["prior_total_balance"]
    payment_days = Decimal(str(line.payment_days_used or 0))
    payment_hours = decimal_hours(line.payment_hours_used)
    money_payment_hours = decimal_hours(line.money_payment_hours_used)

    line.accrued_day_balance = (
        prior_day_balance + Decimal(str(special_days_generated)) + manual_day_adjustment - payment_days
    ).quantize(TWO_DECIMALS)
    line.accrued_hour_balance = (
        prior_hour_balance + line.overtime_hours + manual_hour_adjustment - payment_hours - money_payment_hours
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

    if payment_days > max(prior_day_balance, Decimal("0.00")):
        warnings.append("Se intento aplicar pago dia sin saldo previo suficiente.")

    if payment_hours + money_payment_hours > max(prior_hour_balance, Decimal("0.00")):
        warnings.append("Las horas descontadas superan el saldo previo disponible.")

    if line.accrued_day_balance < Decimal("0.00"):
        warnings.append("El saldo de dias queda a favor de la empresa.")

    if line.accrued_hour_balance < Decimal("0.00"):
        warnings.append("El saldo de horas queda a favor de la empresa.")

    # Mantiene consistencia con datos anteriores, pero ya no se diligencian manualmente.
    line.pending_dates_note = ""
    line.pending_days = Decimal("0.00")
    line.pending_hours = Decimal("0.00")
    line.pending_hours_variance = line.accrued_total_hours_balance
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
                    quantity_days=Decimal("-1.00"),
                    quantity_hours=Decimal("0.00"),
                    equivalent_hours=(day_reference_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    description="Pago con descanso",
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
    touched_employee_identifiers: list[str] = []

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
        save_schedule_line_with_balances(line)
        touched_employee_identifiers.append(line.employee_identifier)

    if touched_employee_identifiers:
        rebuild_balances_for_employees_from_week(schedule.week_start_date, touched_employee_identifiers)

    return created_count, updated_count
