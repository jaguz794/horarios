from __future__ import annotations

import csv
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
import re
import unicodedata

from core.models import JobRole, OperationalStaffCache, ShiftTemplate, Site, SystemConfiguration
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.db.utils import OperationalError, ProgrammingError
from legacy.services import OperationalStaffingRecord, fetch_active_staff_for_site
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

MONEY_HOUR_COMPENSATION_MODES = {
    ScheduleLine.CompensationMode.PAY_MONEY,
    ScheduleLine.CompensationMode.PAY_MONEY_HOURS,
}
MONEY_DAY_COMPENSATION_MODES = {
    ScheduleLine.CompensationMode.PAY_MONEY_DAY,
}
ADVANCE_DAY_COMPENSATION_MODES = {
    ScheduleLine.CompensationMode.ADVANCE_DAY,
}
_INITIAL_BALANCE_REBUILD_SUPPRESSION = ContextVar("initial_balance_rebuild_suppression", default=0)

REST_SHIFT_ALIASES = {
    REST_SHIFT_LABEL,
}
LEAVE_SHIFT_LABELS = {
    "incapacidad",
    "traslado",
    "vacaciones",
    "renuncia",
    "licencia",
}


@contextmanager
def suppress_initial_balance_rebuild():
    token = _INITIAL_BALANCE_REBUILD_SUPPRESSION.set(_INITIAL_BALANCE_REBUILD_SUPPRESSION.get() + 1)
    try:
        yield
    finally:
        _INITIAL_BALANCE_REBUILD_SUPPRESSION.reset(token)


def initial_balance_rebuild_is_suppressed() -> bool:
    return _INITIAL_BALANCE_REBUILD_SUPPRESSION.get() > 0


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
    weekly_target_hours = decimal_hours(line.weekly_target_hours or config.default_weekly_hours or "0")
    if weekly_target_hours > Decimal("0.00"):
        reference_hours = weekly_target_hours / Decimal("6")
    else:
        reference_hours = line.daily_max_hours or config.default_daily_max_hours or Decimal("0.00")
    return decimal_hours(reference_hours)


def get_shift_non_work_category(
    label: str,
    *,
    shift_templates: dict[str, ShiftTemplate] | None = None,
) -> str:
    normalized = normalize_shift_label(label)
    if not normalized:
        return ""

    lowered = normalized.casefold()
    if lowered in REST_SHIFT_ALIASES:
        return "rest"
    if lowered in LEAVE_SHIFT_LABELS:
        return "leave"

    template = None
    if shift_templates is not None:
        template = shift_templates.get(normalized)
    if template is not None and not template.counts_as_worked_time:
        if lowered in REST_SHIFT_ALIASES:
            return "rest"
        return "leave"

    return ""


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


def schedule_accepts_blacklisted_staff(schedule: WeeklySchedule | None) -> bool:
    site = getattr(schedule, "site", None)
    return bool(site and site.is_personal_vario)


def build_personal_vario_staff_records() -> list[OperationalStaffingRecord]:
    blacklist_entries = list(
        EmployeeScheduleBlacklist.objects.filter(is_active=True).order_by("employee_name", "employee_identifier")
    )
    if not blacklist_entries:
        return []

    identifiers = [entry.employee_identifier for entry in blacklist_entries if (entry.employee_identifier or "").strip()]
    cache_rows = list(
        OperationalStaffCache.objects.filter(employee_identifier__in=identifiers, is_active=True).order_by(
            "employee_identifier",
            "role_name",
            "employee_name",
        )
    )
    cache_map: dict[str, OperationalStaffCache] = {}
    for row in cache_rows:
        cache_map.setdefault((row.employee_identifier or "").strip(), row)

    records = []
    for entry in blacklist_entries:
        employee_identifier = (entry.employee_identifier or "").strip()
        if not employee_identifier:
            continue
        cached_row = cache_map.get(employee_identifier)
        employee_name = (
            (entry.employee_name or "").strip()
            or ((cached_row.employee_name or "").strip() if cached_row else "")
            or employee_identifier
        )
        role_code = ((cached_row.role_code or "").strip() if cached_row else "")
        role_name = ((cached_row.role_name or "").strip() if cached_row else "") or "PERSONAL VARIO"
        department_code = ((cached_row.department_code or "").strip() if cached_row else "")
        department_name = ((cached_row.department_name or "").strip() if cached_row else "")
        records.append(
            OperationalStaffingRecord(
                employee_id=employee_identifier,
                employee_name=employee_name,
                site_code=Site.PERSONAL_VARIO_CODE,
                department_code=department_code,
                department_name=department_name,
                role_code=role_code,
                role_name=role_name,
            )
        )

    return sorted(
        records,
        key=lambda item: (
            (item.role_name or "").casefold(),
            (item.employee_name or "").casefold(),
            (item.employee_id or "").casefold(),
        ),
    )


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
                "shift_1_label": shift_1_label,
                "shift_2_label": shift_2_label,
                "worked_hours": (shift_1_hours + shift_2_hours).quantize(TWO_DECIMALS),
                "night_hours": (shift_1_night + shift_2_night).quantize(TWO_DECIMALS),
                "special_label": get_special_day_label(day_date),
                "compensation_mode": compensation_mode,
                "compensation_hours": compensation_hours,
            }
        )

    return breakdown


def get_weekly_rest_day_index(
    line: ScheduleLine,
    day_breakdown: list[dict[str, Decimal | date | str | int]],
    *,
    shift_templates: dict[str, ShiftTemplate] | None = None,
) -> int:
    sunday_info = next((item for item in day_breakdown if int(item["index"]) == 0), None)
    sunday_worked = decimal_hours((sunday_info or {}).get("worked_hours", Decimal("0.00"))) > Decimal("0.00")
    if not sunday_worked:
        return 0

    for day_info in day_breakdown:
        index = int(day_info["index"])
        if index == 0:
            continue
        if decimal_hours(day_info["worked_hours"]) > Decimal("0.00"):
            continue
        compensation_mode = str(day_info.get("compensation_mode") or "")
        if compensation_mode in {ScheduleLine.CompensationMode.PAY_DAY, *ADVANCE_DAY_COMPENSATION_MODES}:
            continue
        shift_categories = {
            get_shift_non_work_category(str(day_info.get("shift_1_label") or ""), shift_templates=shift_templates),
            get_shift_non_work_category(str(day_info.get("shift_2_label") or ""), shift_templates=shift_templates),
        }
        if "rest" in shift_categories:
            return index

    return 0


def build_expected_week_plan(
    line: ScheduleLine,
    *,
    day_breakdown: list[dict[str, Decimal | date | str | int]] | None = None,
    config: SystemConfiguration | None = None,
    shift_templates: dict[str, ShiftTemplate] | None = None,
) -> dict[str, object]:
    config = config or SystemConfiguration.load()
    day_breakdown = day_breakdown or build_line_day_breakdown(line, config=config, shift_templates=shift_templates)
    shift_templates = shift_templates or get_selected_shift_templates(line)
    day_reference_hours = get_line_day_reference_hours(line, config=config)
    weekly_target_hours = decimal_hours(line.weekly_target_hours or config.default_weekly_hours or "0")
    mandatory_rest_index = get_weekly_rest_day_index(line, day_breakdown, shift_templates=shift_templates)
    scope_indexes = get_schedule_line_scope_indexes(line)
    day_plans: list[dict[str, object]] = []
    pending_expected_indexes: list[int] = []

    for day_info in day_breakdown:
        index = int(day_info["index"])
        day_date = day_info["date"]
        compensation_mode = str(day_info.get("compensation_mode") or "")
        special_label = str(day_info.get("special_label") or "")
        is_non_sunday_holiday = day_date.weekday() != 6 and "Festivo" in special_label
        shift_categories = {
            get_shift_non_work_category(str(day_info.get("shift_1_label") or ""), shift_templates=shift_templates),
            get_shift_non_work_category(str(day_info.get("shift_2_label") or ""), shift_templates=shift_templates),
        }
        is_leave_day = (
            decimal_hours(day_info["worked_hours"]) == Decimal("0.00")
            and "leave" in shift_categories
            and not is_non_sunday_holiday
            and compensation_mode not in {ScheduleLine.CompensationMode.PAY_DAY, *ADVANCE_DAY_COMPENSATION_MODES}
        )

        expected_hours = day_reference_hours
        expected_reason = "laborable"
        if index not in scope_indexes:
            expected_hours = Decimal("0.00")
            expected_reason = "fuera_de_rango"
        if index == mandatory_rest_index:
            expected_hours = Decimal("0.00")
            expected_reason = "descanso_obligatorio"
        if is_non_sunday_holiday:
            expected_hours = Decimal("0.00")
            expected_reason = "festivo"
        if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
            expected_hours = Decimal("0.00")
            expected_reason = "descanso_compensatorio"
        if compensation_mode in ADVANCE_DAY_COMPENSATION_MODES:
            expected_hours = Decimal("0.00")
            expected_reason = "descanso_adelantado"
        if is_leave_day:
            expected_hours = Decimal("0.00")
            expected_reason = "novedad_no_laborable"

        if expected_hours > Decimal("0.00"):
            pending_expected_indexes.append(index)

        day_plans.append(
            {
                "index": index,
                "date": day_date,
                "expected_hours": decimal_hours(expected_hours),
                "expected_reason": expected_reason,
                "is_non_sunday_holiday": is_non_sunday_holiday,
                "is_leave_day": is_leave_day,
                "is_mandatory_rest_day": index == mandatory_rest_index,
                "is_advance_rest_day": compensation_mode in ADVANCE_DAY_COMPENSATION_MODES,
                "is_compensatory_rest_day": compensation_mode == ScheduleLine.CompensationMode.PAY_DAY,
            }
        )

    expected_work_days = len(pending_expected_indexes)
    if expected_work_days > 0:
        if weekly_target_hours > Decimal("0.00"):
            expected_weekly_hours = decimal_hours(
                (weekly_target_hours * Decimal(str(expected_work_days))) / Decimal("6")
            )
        else:
            expected_weekly_hours = decimal_hours(day_reference_hours * Decimal(str(expected_work_days)))

        total_cents = int((expected_weekly_hours * Decimal("100")).quantize(Decimal("1")))
        base_cents, remainder_cents = divmod(total_cents, expected_work_days)
        expected_hours_by_index = {
            index: Decimal(base_cents + (1 if position < remainder_cents else 0)) / Decimal("100")
            for position, index in enumerate(pending_expected_indexes)
        }
        for day_plan in day_plans:
            day_plan["expected_hours"] = expected_hours_by_index.get(int(day_plan["index"]), Decimal("0.00"))
    else:
        expected_weekly_hours = Decimal("0.00")

    return {
        "mandatory_rest_index": mandatory_rest_index,
        "day_reference_hours": day_reference_hours,
        "expected_work_days": expected_work_days,
        "expected_weekly_hours": expected_weekly_hours,
        "day_plans": day_plans,
    }


def build_compensation_entries(
    line: ScheduleLine,
    *,
    day_breakdown: list[dict[str, Decimal | date | str | int]] | None = None,
    expected_plan: dict[str, object] | None = None,
    config: SystemConfiguration | None = None,
    shift_templates: dict[str, ShiftTemplate] | None = None,
) -> list[dict[str, Decimal | int | str | bool]]:
    config = config or SystemConfiguration.load()
    day_breakdown = day_breakdown or build_line_day_breakdown(line, config=config, shift_templates=shift_templates)
    expected_plan = expected_plan or build_expected_week_plan(
        line,
        day_breakdown=day_breakdown,
        config=config,
        shift_templates=shift_templates,
    )
    expected_hours_by_index = {
        int(day_plan["index"]): decimal_hours(day_plan["expected_hours"])
        for day_plan in expected_plan["day_plans"]
    }

    return [
        {
            "index": int(day_info["index"]),
            "mode": str(day_info["compensation_mode"] or ""),
            "hours": decimal_hours(day_info["compensation_hours"]),
            "worked_hours": decimal_hours(day_info["worked_hours"]),
            "expected_hours": expected_hours_by_index.get(int(day_info["index"]), Decimal("0.00")),
            "special_generated": bool(
                decimal_hours(day_info["worked_hours"]) > Decimal("0.00") and day_info["special_label"]
            ),
        }
        for day_info in day_breakdown
    ]


def schedule_day_is_special(line: ScheduleLine, index: int) -> bool:
    schedule = getattr(line, "schedule", None)
    if schedule is None or not schedule.week_start_date:
        return False
    day_date = schedule.week_start_date + timedelta(days=index)
    return bool(get_special_day_label(day_date))


def get_schedule_line_activity_indices(line: ScheduleLine) -> list[int]:
    activity_indices: list[int] = []
    for index in range(7):
        shift_1_label = normalize_shift_label(getattr(line, f"day_{index}_shift_1", "") or "")
        shift_2_label = normalize_shift_label(getattr(line, f"day_{index}_shift_2", "") or "")
        compensation_mode = str(getattr(line, f"day_{index}_compensation_mode", "") or "").strip()
        compensation_hours = decimal_hours(
            getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0"
        )
        if shift_1_label or shift_2_label or compensation_mode or compensation_hours != Decimal("0.00"):
            activity_indices.append(index)
    return activity_indices


def get_schedule_line_progression_key(line: ScheduleLine) -> tuple[date, int, int, datetime, int, int]:
    schedule = getattr(line, "schedule", None)
    week_start = getattr(schedule, "week_start_date", None) or date.min
    schedule_created_at = getattr(schedule, "created_at", None) or datetime.min
    activity_indices = get_schedule_line_activity_indices(line)
    if activity_indices:
        first_activity_index = activity_indices[0]
        last_activity_index = activity_indices[-1]
    else:
        first_activity_index = 7
        last_activity_index = 7
    return (
        week_start,
        first_activity_index,
        last_activity_index,
        schedule_created_at,
        getattr(schedule, "pk", 0) or 0,
        getattr(line, "pk", 0) or 0,
    )


def get_schedule_line_scope_indexes(line: ScheduleLine) -> set[int]:
    schedule = getattr(line, "schedule", None)
    employee_identifier = (getattr(line, "employee_identifier", "") or "").strip()
    if schedule is None or not schedule.week_start_date or not employee_identifier:
        return set(range(7))

    same_week_lines = list(
        ScheduleLine.objects.select_related("schedule").filter(
            employee_identifier=employee_identifier,
            schedule__week_start_date=schedule.week_start_date,
        )
    )
    if not any(getattr(candidate, "pk", None) == getattr(line, "pk", None) for candidate in same_week_lines):
        same_week_lines.append(line)

    ordered_lines = sorted(same_week_lines, key=get_schedule_line_progression_key)
    if len(ordered_lines) <= 1:
        return set(range(7))

    activity_meta = [
        {
            "line": candidate,
            "activity_indices": get_schedule_line_activity_indices(candidate),
        }
        for candidate in ordered_lines
    ]
    current_position = next(
        (
            index
            for index, item in enumerate(activity_meta)
            if getattr(item["line"], "pk", None) == getattr(line, "pk", None)
        ),
        None,
    )
    if current_position is None:
        return set(range(7))

    current_activity = activity_meta[current_position]["activity_indices"]
    if not current_activity:
        return set()

    has_previous_active_line = any(item["activity_indices"] for item in activity_meta[:current_position])
    scope_start = current_activity[0] if has_previous_active_line else 0
    next_active_first = next(
        (
            item["activity_indices"][0]
            for item in activity_meta[current_position + 1 :]
            if item["activity_indices"]
        ),
        None,
    )
    scope_end = 6 if next_active_first is None else max(current_activity[-1], next_active_first - 1)
    return {index for index in range(scope_start, scope_end + 1)}


def resolve_compensation_usage(
    compensation_entries: list[dict[str, Decimal | int | str]],
    *,
    available_day_balance: Decimal,
    available_hour_balance: Decimal,
    available_advance_pending_balance: Decimal = Decimal("0.00"),
    day_reference_hours: Decimal,
    weekly_target_hours: Decimal | None = None,
) -> dict[str, Decimal | int | list[int] | dict[int, dict[str, Decimal | str | bool]]]:
    remaining_day_balance = decimal_hours(available_day_balance)
    remaining_hour_balance = decimal_hours(available_hour_balance)
    remaining_advance_pending_balance = max(decimal_hours(available_advance_pending_balance), Decimal("0.00"))

    payment_days_used = 0
    advance_rest_days_used = 0
    payment_days_from_day_balance = 0
    uncovered_payment_days = 0
    money_payment_days_used = 0
    payment_hours_used = Decimal("0.00")
    money_payment_hours_used = Decimal("0.00")
    invalid_pay_day_indices: list[int] = []
    invalid_pay_hours_indices: list[int] = []
    invalid_pay_money_day_indices: list[int] = []
    invalid_pay_money_indices: list[int] = []
    invalid_advance_day_indices: list[int] = []
    invalid_advance_day_with_balance_indices: list[int] = []
    day_states: dict[int, dict[str, Decimal | str | bool]] = {}

    for entry in sorted(compensation_entries, key=lambda item: int(item.get("index", 0))):
        index = int(entry.get("index", 0))
        compensation_mode = str(entry.get("mode") or "")
        compensation_hours = decimal_hours(entry.get("hours", Decimal("0.00")) or "0")
        worked_hours = decimal_hours(entry.get("worked_hours", Decimal("0.00")) or "0")
        expected_hours = decimal_hours(entry.get("expected_hours", Decimal("0.00")) or "0")
        special_generated = bool(entry.get("special_generated"))
        day_state: dict[str, Decimal | str | bool] = {
            "mode": compensation_mode,
            "requested_hours": compensation_hours,
            "source": "",
            "valid": True,
            "available_day_balance": max(remaining_day_balance, Decimal("0.00")).quantize(TWO_DECIMALS),
            "available_hour_balance": max(remaining_hour_balance, Decimal("0.00")).quantize(TWO_DECIMALS),
            "available_advance_pending_balance": remaining_advance_pending_balance.quantize(TWO_DECIMALS),
            "remaining_day_balance": remaining_day_balance,
            "remaining_hour_balance": remaining_hour_balance,
            "remaining_advance_pending_balance": remaining_advance_pending_balance,
            "generated_day": False,
            "generated_hours": Decimal("0.00"),
            "hour_difference": Decimal("0.00"),
        }

        if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
            payment_days_used += 1
            if remaining_day_balance >= Decimal("1.00"):
                remaining_day_balance = (remaining_day_balance - Decimal("1.00")).quantize(TWO_DECIMALS)
                payment_days_from_day_balance += 1
                day_state["source"] = "day_balance"
            else:
                uncovered_payment_days += 1
                invalid_pay_day_indices.append(index)
                day_state["source"] = "insufficient"
                day_state["valid"] = False
        elif compensation_mode in MONEY_DAY_COMPENSATION_MODES:
            money_payment_days_used += 1
            if remaining_day_balance >= Decimal("1.00"):
                remaining_day_balance = (remaining_day_balance - Decimal("1.00")).quantize(TWO_DECIMALS)
                day_state["source"] = "day_balance"
            else:
                invalid_pay_money_day_indices.append(index)
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
        elif compensation_mode in MONEY_HOUR_COMPENSATION_MODES:
            money_payment_hours_used += compensation_hours
            remaining_before = remaining_hour_balance
            remaining_hour_balance = (remaining_hour_balance - compensation_hours).quantize(TWO_DECIMALS)
            if compensation_hours <= Decimal("0.00") or remaining_before < compensation_hours:
                invalid_pay_money_indices.append(index)
                day_state["valid"] = False
            day_state["source"] = "hour_balance"
        elif compensation_mode in ADVANCE_DAY_COMPENSATION_MODES:
            advance_rest_days_used += 1
            if remaining_day_balance >= Decimal("1.00"):
                invalid_advance_day_with_balance_indices.append(index)
                day_state["valid"] = False
                day_state["source"] = "use_pay_day"
            else:
                remaining_day_balance = (remaining_day_balance - Decimal("1.00")).quantize(TWO_DECIMALS)
                remaining_advance_pending_balance = (
                    remaining_advance_pending_balance + Decimal("1.00")
                ).quantize(TWO_DECIMALS)
                day_state["source"] = "advance_rest"

        if special_generated and worked_hours > Decimal("0.00"):
            remaining_day_balance = (remaining_day_balance + Decimal("1.00")).quantize(TWO_DECIMALS)
            day_state["generated_day"] = True
            if remaining_advance_pending_balance > Decimal("0.00"):
                offset_days = min(remaining_advance_pending_balance, Decimal("1.00"))
                remaining_advance_pending_balance = (
                    remaining_advance_pending_balance - offset_days
                ).quantize(TWO_DECIMALS)

        hour_difference = (worked_hours - expected_hours).quantize(TWO_DECIMALS)
        remaining_hour_balance = (remaining_hour_balance + hour_difference).quantize(TWO_DECIMALS)
        if hour_difference > Decimal("0.00"):
            day_state["generated_hours"] = hour_difference
        day_state["hour_difference"] = hour_difference

        day_state["remaining_day_balance"] = remaining_day_balance
        day_state["remaining_hour_balance"] = remaining_hour_balance
        day_state["remaining_advance_pending_balance"] = remaining_advance_pending_balance
        day_states[index] = day_state

    return {
        "payment_days_used": payment_days_used,
        "advance_rest_days_used": advance_rest_days_used,
        "payment_days_from_day_balance": payment_days_from_day_balance,
        "payment_days_from_hour_balance": 0,
        "uncovered_payment_days": uncovered_payment_days,
        "money_payment_days_used": money_payment_days_used,
        "payment_day_hour_equivalent": Decimal("0.00"),
        "payment_hours_used": payment_hours_used.quantize(TWO_DECIMALS),
        "money_payment_hours_used": money_payment_hours_used.quantize(TWO_DECIMALS),
        "invalid_pay_day_indices": invalid_pay_day_indices,
        "invalid_pay_hours_indices": invalid_pay_hours_indices,
        "invalid_pay_money_day_indices": invalid_pay_money_day_indices,
        "invalid_pay_money_indices": invalid_pay_money_indices,
        "invalid_advance_day_indices": invalid_advance_day_indices,
        "invalid_advance_day_with_balance_indices": invalid_advance_day_with_balance_indices,
        "remaining_day_balance": remaining_day_balance.quantize(TWO_DECIMALS),
        "remaining_hour_balance": remaining_hour_balance.quantize(TWO_DECIMALS),
        "remaining_advance_pending_balance": remaining_advance_pending_balance.quantize(TWO_DECIMALS),
        "day_states": day_states,
    }


def summarize_line_payments(line: ScheduleLine) -> tuple[int, int, Decimal, int, Decimal]:
    payment_days_used = 0
    advance_rest_days_used = 0
    payment_hours_used = Decimal("0.00")
    money_payment_days_used = 0
    money_payment_hours_used = Decimal("0.00")

    for index in range(7):
        compensation_mode = getattr(line, f"day_{index}_compensation_mode", "") or ""
        compensation_hours = decimal_hours(
            getattr(line, f"day_{index}_compensation_hours", Decimal("0.00")) or "0"
        )

        if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
            payment_days_used += 1
        elif compensation_mode in ADVANCE_DAY_COMPENSATION_MODES:
            advance_rest_days_used += 1
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
            payment_hours_used += compensation_hours
        elif compensation_mode in MONEY_DAY_COMPENSATION_MODES:
            money_payment_days_used += 1
        elif compensation_mode in MONEY_HOUR_COMPENSATION_MODES:
            money_payment_hours_used += compensation_hours

    return (
        payment_days_used,
        advance_rest_days_used,
        payment_hours_used.quantize(TWO_DECIMALS),
        money_payment_days_used,
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
        "prior_advance_pending_balance": Decimal("0.00"),
        "prior_day_equivalent_hours": Decimal("0.00"),
        "day_reference_hours": get_line_day_reference_hours(line, config=config),
    }

    employee_identifier = (line.employee_identifier or "").strip()
    schedule = getattr(line, "schedule", None)
    if not employee_identifier or schedule is None or not schedule.week_start_date:
        return zero_balance

    current_key = get_schedule_line_progression_key(line)
    previous_candidates = list(
        ScheduleLine.objects.filter(
            employee_identifier=employee_identifier,
            schedule__week_start_date__lte=schedule.week_start_date,
        )
        .exclude(pk=getattr(line, "pk", None))
        .select_related("schedule")
    )
    previous_line = None
    previous_key = None
    for candidate in previous_candidates:
        candidate_key = get_schedule_line_progression_key(candidate)
        if candidate_key >= current_key:
            continue
        if previous_key is None or candidate_key > previous_key:
            previous_line = candidate
            previous_key = candidate_key

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
            "prior_advance_pending_balance": Decimal("0.00"),
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
        "prior_advance_pending_balance": decimal_hours(previous_line.advance_rest_pending_balance),
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

    with suppress_initial_balance_rebuild():
        for row_number, row in enumerate(data_rows, start=2):
            identifier = str(row[identifier_index] or "").strip().upper() if identifier_index < len(row) else ""
            if not identifier:
                continue
            employee_name = (
                str(row[name_index] or "").strip()
                if name_index is not None and name_index < len(row)
                else ""
            )
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

    touched_identifiers = sorted({identifier for identifier in touched_identifiers if identifier})

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
    shift_templates = get_selected_shift_templates(line)
    day_breakdown = build_line_day_breakdown(line, config=config, shift_templates=shift_templates)
    expected_plan = build_expected_week_plan(
        line,
        day_breakdown=day_breakdown,
        config=config,
        shift_templates=shift_templates,
    )
    compensation_entries = build_compensation_entries(
        line,
        day_breakdown=day_breakdown,
        expected_plan=expected_plan,
        config=config,
        shift_templates=shift_templates,
    )
    manual_day_adjustment = decimal_hours(line.manual_day_adjustment)
    manual_hour_adjustment = decimal_hours(line.manual_hour_adjustment)
    payment_resolution = resolve_compensation_usage(
        compensation_entries,
        available_day_balance=balance_snapshot["prior_day_balance"] + manual_day_adjustment,
        available_hour_balance=balance_snapshot["prior_hour_balance"] + manual_hour_adjustment,
        available_advance_pending_balance=max(
            balance_snapshot["prior_advance_pending_balance"] - max(manual_day_adjustment, Decimal("0.00")),
            Decimal("0.00"),
        ),
        day_reference_hours=balance_snapshot["day_reference_hours"],
        weekly_target_hours=line.expected_weekly_hours or line.weekly_target_hours or config.default_weekly_hours,
    )
    categories: list[str] = []
    daily_limit = line.daily_max_hours or config.default_daily_max_hours
    overtime_restriction = get_active_overtime_restriction(line.employee_identifier)
    daily_restriction_limit = (
        decimal_hours(overtime_restriction.max_daily_overtime_hours)
        if overtime_restriction is not None
        else Decimal("0.00")
    )

    if any(decimal_hours(day_info["worked_hours"]) > daily_limit for day_info in day_breakdown):
        categories.append("limites del dia")

    if overtime_restriction and any(
        get_daily_overtime_hours(
            day_info["worked_hours"],
            balance_snapshot["day_reference_hours"],
        ) > daily_restriction_limit
        for day_info in day_breakdown
    ):
        categories.append("restriccion medica")

    if decimal_hours(line.weekly_hour_difference) < Decimal("0.00"):
        categories.append("horas esperadas")

    if (
        overtime_restriction
        and decimal_hours(line.overtime_hours) > decimal_hours(overtime_restriction.max_weekly_overtime_hours)
    ):
        categories.append("restriccion medica")

    if (
        payment_resolution["invalid_pay_day_indices"]
        or payment_resolution["invalid_pay_hours_indices"]
        or payment_resolution["invalid_pay_money_day_indices"]
        or payment_resolution["invalid_pay_money_indices"]
        or payment_resolution["invalid_advance_day_with_balance_indices"]
    ):
        categories.append("saldo previo")

    if decimal_hours(line.advance_rest_pending_balance) > Decimal("0.00"):
        categories.append("descansos adelantados")

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
    expected_plan = build_expected_week_plan(
        line,
        day_breakdown=day_breakdown,
        config=config,
        shift_templates=shift_templates,
    )
    compensation_entries = build_compensation_entries(
        line,
        day_breakdown=day_breakdown,
        expected_plan=expected_plan,
        config=config,
        shift_templates=shift_templates,
    )

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
        is_special_day = bool(day_info["special_label"])

        if compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
            compensated_day_hours = daily_hours + compensation_hours
            if compensation_hours <= Decimal("0.00"):
                warnings.append(f"Dia {index + 1}: pago horas requiere una cantidad mayor que cero.")
            elif daily_hours >= day_reference_hours:
                warnings.append(f"Dia {index + 1}: la jornada ya esta completa y no requiere pago horas.")
            elif compensated_day_hours > day_reference_hours:
                warnings.append(f"Dia {index + 1}: pago horas supera la jornada del dia.")
        elif compensation_mode == ScheduleLine.CompensationMode.PAY_DAY and is_special_day:
            warnings.append(f"Dia {index + 1}: no se pueden pagar dias acumulados en domingo o festivo.")
        elif compensation_mode in MONEY_DAY_COMPENSATION_MODES:
            continue
        elif compensation_mode in MONEY_HOUR_COMPENSATION_MODES and compensation_hours <= Decimal("0.00"):
            warnings.append(f"Dia {index + 1}: pago en dinero por horas requiere una cantidad mayor que cero.")

    line.total_hours = total_hours.quantize(TWO_DECIMALS)
    line.expected_work_days = int(expected_plan["expected_work_days"])
    line.expected_weekly_hours = decimal_hours(expected_plan["expected_weekly_hours"])
    line.weekly_hour_difference = (line.total_hours - line.expected_weekly_hours).quantize(TWO_DECIMALS)
    line.overtime_hours = max(line.weekly_hour_difference, Decimal("0.00")).quantize(TWO_DECIMALS)
    line.night_bonus_hours = total_night_bonus.quantize(TWO_DECIMALS)
    line.special_days_generated = special_days_generated
    (
        line.payment_days_used,
        line.advance_rest_days_used,
        line.payment_hours_used,
        line.money_payment_days_used,
        line.money_payment_hours_used,
    ) = summarize_line_payments(line)

    manual_day_adjustment = decimal_hours(line.manual_day_adjustment)
    manual_hour_adjustment = decimal_hours(line.manual_hour_adjustment)
    prior_day_balance = balance_snapshot["prior_day_balance"]
    prior_hour_balance = balance_snapshot["prior_hour_balance"]
    payment_resolution = resolve_compensation_usage(
        compensation_entries,
        available_day_balance=prior_day_balance + manual_day_adjustment,
        available_hour_balance=prior_hour_balance + manual_hour_adjustment,
        available_advance_pending_balance=max(
            balance_snapshot["prior_advance_pending_balance"] - max(manual_day_adjustment, Decimal("0.00")),
            Decimal("0.00"),
        ),
        day_reference_hours=day_reference_hours,
        weekly_target_hours=weekly_target,
    )
    payment_days = Decimal(str(line.payment_days_used or 0))
    advance_rest_days = Decimal(str(line.advance_rest_days_used or 0))
    money_payment_days = Decimal(str(line.money_payment_days_used or 0))
    payment_hours = decimal_hours(payment_resolution["payment_hours_used"])
    money_payment_hours = decimal_hours(payment_resolution["money_payment_hours_used"])

    line.accrued_day_balance = (
        prior_day_balance
        + Decimal(str(special_days_generated))
        + manual_day_adjustment
        - payment_days
        - money_payment_days
        - advance_rest_days
    ).quantize(TWO_DECIMALS)
    line.accrued_hour_balance = (
        prior_hour_balance
        + line.weekly_hour_difference
        + manual_hour_adjustment
        - payment_hours
        - money_payment_hours
    ).quantize(TWO_DECIMALS)
    line.advance_rest_pending_balance = decimal_hours(payment_resolution["remaining_advance_pending_balance"])
    line.accrued_total_hours_balance = (
        line.accrued_hour_balance + (line.accrued_day_balance * day_reference_hours)
    ).quantize(TWO_DECIMALS)

    if line.weekly_hour_difference < Decimal("0.00"):
        warnings.append(
            "No cumple las horas esperadas de la semana: "
            f"{line.total_hours} h programadas vs {line.expected_weekly_hours} h esperadas "
            f"({line.weekly_hour_difference} h)."
        )

    if (
        overtime_restriction
        and line.overtime_hours > weekly_restriction_limit
    ):
        warnings.append(
            "Restriccion medica: no puede superar "
            f"{weekly_restriction_limit} h extra en la semana."
        )

    if payment_resolution["invalid_pay_day_indices"]:
        warnings.append("Se intento aplicar pago dia sin un dia acumulado disponible.")

    if payment_resolution["invalid_pay_money_day_indices"]:
        warnings.append("Se intento aplicar pago en dinero por dia sin un dia acumulado disponible.")

    if payment_resolution["invalid_advance_day_with_balance_indices"]:
        warnings.append("Hay descansos adelantados marcados en dias donde ya existe saldo positivo; usa pago dia.")

    if payment_resolution["invalid_pay_hours_indices"] or payment_resolution["invalid_pay_money_indices"]:
        warnings.append("Las horas descontadas superan el saldo acumulado disponible.")

    if line.accrued_day_balance < Decimal("0.00"):
        warnings.append("El saldo de dias queda a favor de la empresa.")

    if line.accrued_hour_balance < Decimal("0.00"):
        warnings.append("El saldo de horas queda a favor de la empresa.")

    if line.advance_rest_pending_balance > Decimal("0.00"):
        warnings.append(
            f"Descansos adelantados pendientes por cruzar: {line.advance_rest_pending_balance} dia(s)."
        )

    line.validation_summary = " ".join(warnings)
    line.warnings_count = len(warnings)
    return line


def rebuild_schedule_line_movements(line: ScheduleLine) -> None:
    if not line.pk or not line.schedule_id:
        return

    config = SystemConfiguration.load()
    day_reference_hours = get_line_day_reference_hours(line, config=config)
    shift_templates = get_selected_shift_templates(line)
    day_breakdown = build_line_day_breakdown(line, config=config, shift_templates=shift_templates)
    expected_plan = build_expected_week_plan(
        line,
        day_breakdown=day_breakdown,
        config=config,
        shift_templates=shift_templates,
    )
    compensation_entries = build_compensation_entries(
        line,
        day_breakdown=day_breakdown,
        expected_plan=expected_plan,
        config=config,
        shift_templates=shift_templates,
    )
    movement_date_default = line.schedule.week_end_date or line.schedule.week_start_date
    movements: list[ScheduleBalanceMovement] = []

    entries_by_index = {int(entry["index"]): entry for entry in compensation_entries}

    for day_info in day_breakdown:
        day_date = day_info["date"]
        worked_hours = decimal_hours(day_info["worked_hours"])
        special_label = str(day_info["special_label"] or "")
        compensation_mode = str(day_info["compensation_mode"] or "")
        compensation_hours = decimal_hours(day_info["compensation_hours"])
        compensation_entry = entries_by_index.get(int(day_info["index"]), {})

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
        elif compensation_mode in ADVANCE_DAY_COMPENSATION_MODES:
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=ScheduleBalanceMovement.MovementType.ADVANCE_DAY,
                    quantity_days=Decimal("-1.00"),
                    quantity_hours=Decimal("0.00"),
                    equivalent_hours=(day_reference_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    description="Descanso adelantado",
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
        elif compensation_mode in MONEY_DAY_COMPENSATION_MODES:
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=ScheduleBalanceMovement.MovementType.PAY_MONEY_DAY,
                    quantity_days=Decimal("-1.00"),
                    quantity_hours=Decimal("0.00"),
                    equivalent_hours=(day_reference_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    description="Pago en dinero por dia",
                )
            )
        elif compensation_mode in MONEY_HOUR_COMPENSATION_MODES and compensation_hours != Decimal("0.00"):
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=ScheduleBalanceMovement.MovementType.PAY_MONEY_HOURS,
                    quantity_days=Decimal("0.00"),
                    quantity_hours=(compensation_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    equivalent_hours=(compensation_hours * Decimal("-1.00")).quantize(TWO_DECIMALS),
                    description="Pago en dinero por horas",
                )
            )

        hour_difference = (
            decimal_hours(compensation_entry.get("worked_hours", Decimal("0.00")) or "0")
            - decimal_hours(compensation_entry.get("expected_hours", Decimal("0.00")) or "0")
        ).quantize(TWO_DECIMALS)
        if hour_difference != Decimal("0.00"):
            movements.append(
                ScheduleBalanceMovement(
                    schedule=line.schedule,
                    line=line,
                    site=line.schedule.site,
                    employee_identifier=line.employee_identifier,
                    employee_name=line.employee_name,
                    job_role_name=line.job_role_name,
                    movement_date=day_date,
                    movement_type=(
                        ScheduleBalanceMovement.MovementType.OVERTIME
                        if hour_difference > Decimal("0.00")
                        else ScheduleBalanceMovement.MovementType.HOUR_DEFICIT
                    ),
                    quantity_days=Decimal("0.00"),
                    quantity_hours=hour_difference,
                    equivalent_hours=hour_difference,
                    description=(
                        "Horas adicionales del dia"
                        if hour_difference > Decimal("0.00")
                        else "Horas faltantes del dia"
                    ),
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


def rebuild_balances_for_employee_from_earliest_schedule(employee_identifier: str) -> bool:
    cleaned_identifier = (employee_identifier or "").strip()
    if not cleaned_identifier:
        return False

    first_line = (
        ScheduleLine.objects.select_related("schedule")
        .filter(employee_identifier=cleaned_identifier)
        .order_by("schedule__week_start_date", "pk")
        .first()
    )
    if first_line is None or first_line.schedule is None:
        return False

    rebuild_balances_for_employees_from_week(
        first_line.schedule.week_start_date,
        [cleaned_identifier],
    )
    return True


def rebuild_balances_for_employees_from_week(
    week_start_date: date,
    employee_identifiers: list[str] | set[str] | tuple[str, ...] | None = None,
) -> None:
    queryset = ScheduleLine.objects.select_related("schedule").filter(
        schedule__week_start_date__gte=week_start_date,
    )
    if employee_identifiers:
        queryset = queryset.filter(employee_identifier__in=set(employee_identifiers))

    ordered_lines = sorted(
        list(queryset),
        key=lambda line: (
            (line.employee_identifier or "").strip(),
            *get_schedule_line_progression_key(line),
        ),
    )
    for line in ordered_lines:
        save_schedule_line_with_balances(line)


def get_latest_schedule_lines_by_employee(
    employee_identifiers: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, ScheduleLine]:
    queryset = ScheduleLine.objects.select_related("schedule", "schedule__site")
    if employee_identifiers:
        queryset = queryset.filter(employee_identifier__in=set(employee_identifiers))

    latest_by_employee: dict[str, tuple[tuple[object, ...], ScheduleLine]] = {}
    for line in queryset:
        cleaned_identifier = (line.employee_identifier or "").strip()
        if not cleaned_identifier:
            continue
        candidate_key = get_schedule_line_progression_key(line)
        current_entry = latest_by_employee.get(cleaned_identifier)
        if current_entry is None or candidate_key > current_entry[0]:
            latest_by_employee[cleaned_identifier] = (candidate_key, line)

    return {
        employee_identifier: entry[1]
        for employee_identifier, entry in latest_by_employee.items()
    }


def build_schedule_balance_audit_rows(
    employee_identifiers: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    latest_lines = get_latest_schedule_lines_by_employee(employee_identifiers)
    if not latest_lines:
        return []

    identifiers = list(latest_lines.keys())
    initial_balances = {
        balance.employee_identifier: balance
        for balance in EmployeeInitialBalance.objects.filter(employee_identifier__in=identifiers)
    }
    movement_totals = {
        row["employee_identifier"]: {
            "days": decimal_hours(row["total_days"]),
            "hours": decimal_hours(row["total_hours"]),
        }
        for row in (
            ScheduleBalanceMovement.objects.filter(employee_identifier__in=identifiers)
            .values("employee_identifier")
            .annotate(
                total_days=Coalesce(
                    Sum("quantity_days"),
                    Value(Decimal("0.00"), output_field=DecimalField(max_digits=8, decimal_places=2)),
                ),
                total_hours=Coalesce(
                    Sum("quantity_hours"),
                    Value(Decimal("0.00"), output_field=DecimalField(max_digits=10, decimal_places=2)),
                ),
            )
        )
    }

    rows: list[dict[str, object]] = []
    for employee_identifier, line in latest_lines.items():
        initial_balance = initial_balances.get(employee_identifier)
        totals = movement_totals.get(
            employee_identifier,
            {"days": Decimal("0.00"), "hours": Decimal("0.00")},
        )
        initial_day_balance = decimal_hours(
            getattr(initial_balance, "initial_day_balance", Decimal("0.00"))
        )
        initial_hour_balance = decimal_hours(
            getattr(initial_balance, "initial_hour_balance", Decimal("0.00"))
        )
        audited_day_balance = (initial_day_balance + totals["days"]).quantize(TWO_DECIMALS)
        audited_hour_balance = (initial_hour_balance + totals["hours"]).quantize(TWO_DECIMALS)
        stored_day_balance = decimal_hours(line.accrued_day_balance)
        stored_hour_balance = decimal_hours(line.accrued_hour_balance)
        day_difference = (stored_day_balance - audited_day_balance).quantize(TWO_DECIMALS)
        hour_difference = (stored_hour_balance - audited_hour_balance).quantize(TWO_DECIMALS)

        rows.append(
            {
                "site_code": getattr(line.schedule.site, "code", ""),
                "site_name": getattr(line.schedule.site, "name", ""),
                "job_role_name": line.job_role_name,
                "employee_identifier": employee_identifier,
                "employee_name": line.employee_name,
                "audited_day_balance": audited_day_balance,
                "stored_day_balance": stored_day_balance,
                "day_difference": day_difference,
                "audited_hour_balance": audited_hour_balance,
                "stored_hour_balance": stored_hour_balance,
                "hour_difference": hour_difference,
                "has_difference": (
                    day_difference != Decimal("0.00")
                    or hour_difference != Decimal("0.00")
                ),
            }
        )

    return sorted(
        rows,
        key=lambda row: (
            str(row["site_code"] or ""),
            str(row["job_role_name"] or "").casefold(),
            str(row["employee_name"] or "").casefold(),
            str(row["employee_identifier"] or ""),
        ),
    )


def purge_blacklisted_lines_from_schedule(schedule: WeeklySchedule) -> list[str]:
    if schedule_accepts_blacklisted_staff(schedule):
        return []

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

    blacklisted_identifiers = set()
    if not schedule_accepts_blacklisted_staff(target_schedule):
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

        line.save()
        copied_count += 1
        touched_employee_identifiers.append(employee_identifier)

    if touched_employee_identifiers:
        rebuild_balances_for_employees_from_week(target_schedule.week_start_date, touched_employee_identifiers)

    return copied_count, len(source_lines)


def sync_schedule_from_legacy(schedule: WeeklySchedule) -> tuple[int, int]:
    config = SystemConfiguration.load()
    purge_blacklisted_lines_from_schedule(schedule)
    if schedule.site.is_personal_vario:
        staff = build_personal_vario_staff_records()
        blacklisted_identifiers: set[str] = set()
    else:
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
            name=employee.role_name or "PERSONAL VARIO",
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
        if schedule.site.is_personal_vario:
            if not (line.job_role_name or "").strip() or (line.job_role_name or "").strip().upper() == "PERSONAL VARIO":
                line.job_role_code = employee.role_code or job_role.code
                line.job_role_name = employee.role_name or job_role.name
                line.weekly_target_hours = job_role.weekly_target_hours
                line.daily_max_hours = job_role.daily_max_hours
            else:
                line.job_role_code = line.job_role_code or employee.role_code or job_role.code
                line.weekly_target_hours = line.weekly_target_hours or job_role.weekly_target_hours
                line.daily_max_hours = line.daily_max_hours or job_role.daily_max_hours
        else:
            line.job_role_code = employee.role_code
            line.job_role_name = employee.role_name
            line.weekly_target_hours = job_role.weekly_target_hours
            line.daily_max_hours = job_role.daily_max_hours
        line.save()
        touched_employee_identifiers.append(line.employee_identifier)

    if touched_employee_identifiers:
        rebuild_balances_for_employees_from_week(schedule.week_start_date, touched_employee_identifiers)

    return created_count, updated_count
