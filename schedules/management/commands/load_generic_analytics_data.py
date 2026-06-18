from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Department, JobRole, ShiftTemplate, Site, SystemConfiguration
from schedules.models import ScheduleLine, WeeklySchedule
from schedules.services import save_schedule_line_with_balances

User = get_user_model()
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class SyntheticProfile:
    code: str
    label: str


PROFILES: tuple[SyntheticProfile, ...] = (
    SyntheticProfile("REG", "REGULAR"),
    SyntheticProfile("EXT", "EXTRAS"),
    SyntheticProfile("NOC", "NOCTURNO"),
    SyntheticProfile("PDI", "PENDIENTE DIA"),
    SyntheticProfile("PHR", "PENDIENTE HORAS"),
    SyntheticProfile("INC", "INCAPACIDAD"),
    SyntheticProfile("INA", "INASISTENCIA"),
    SyntheticProfile("REN", "RENUNCIA"),
)

DAY_SHIFT = "08:00-16:00"
DAY_SHIFT_SHORT = "06:00-12:00"
DAY_SHIFT_HALF = "06:30-13:00"
DAY_SHIFT_TWO = "06:00-08:00"
MORNING_SHIFT = "06:00-10:00"
AFTERNOON_SHIFT = "13:00-17:00"
AFTERNOON_SHIFT_LONG = "13:00-18:00"
EVENING_SHIFT = "14:00-22:00"
NIGHT_SHIFT = "18:00-00:00"
REST_SHIFT = "Descanso"
SICK_SHIFT = "Incapacidad"
ABSENCE_SHIFT = "Inasistencia"
RESIGNATION_SHIFT = "Renuncia"
HOLIDAY_SHIFT = "Festivo"

HOLIDAY_REFERENCE_DATES = {
    date(2026, 4, 2),
    date(2026, 4, 3),
    date(2026, 5, 1),
}


def parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def decimal_value(value: str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def align_to_week_start(target: date, week_start_day: int) -> date:
    offset = (target.weekday() - week_start_day) % 7
    return target - timedelta(days=offset)


def ensure_shift_template(
    *,
    label: str,
    start_time: time | None,
    end_time: time | None,
    duration_hours: str,
    night_bonus_hours: str = "0.00",
    counts_as_worked_time: bool = True,
    display_order: int = 9000,
) -> ShiftTemplate:
    shift, _ = ShiftTemplate.objects.update_or_create(
        label=label,
        defaults={
            "code": label.replace(":", "").replace("-", "_").replace(" ", "_").upper()[:30],
            "start_time": start_time,
            "end_time": end_time,
            "duration_hours": decimal_value(duration_hours),
            "night_bonus_hours": decimal_value(night_bonus_hours),
            "display_order": display_order,
            "counts_as_worked_time": counts_as_worked_time,
            "is_active": True,
        },
    )
    return shift


def initialize_line_fields() -> dict[str, str | Decimal]:
    fields: dict[str, str | Decimal] = {
        "employee_document_type": "",
        "pending_dates_note": "",
        "pending_days": ZERO,
        "pending_hours": ZERO,
    }
    for day_index in range(7):
        fields[f"day_{day_index}_shift_1"] = ""
        fields[f"day_{day_index}_shift_2"] = ""
        fields[f"day_{day_index}_compensation_mode"] = ""
        fields[f"day_{day_index}_compensation_hours"] = ZERO
    return fields


def set_day(
    payload: dict[str, str | Decimal],
    day_index: int,
    shift_1: str = "",
    shift_2: str = "",
    compensation_mode: str = "",
    compensation_hours: str = "0.00",
) -> None:
    payload[f"day_{day_index}_shift_1"] = shift_1
    payload[f"day_{day_index}_shift_2"] = shift_2
    payload[f"day_{day_index}_compensation_mode"] = compensation_mode
    payload[f"day_{day_index}_compensation_hours"] = decimal_value(compensation_hours)


def apply_regular_week(payload: dict[str, str | Decimal]) -> None:
    set_day(payload, 0, REST_SHIFT)
    for day_index in range(1, 6):
        set_day(payload, day_index, DAY_SHIFT)
    set_day(payload, 6, DAY_SHIFT_TWO)


def find_weekday_index_for_date(week_start: date, target_date: date) -> int | None:
    if target_date < week_start or target_date > (week_start + timedelta(days=6)):
        return None
    return (target_date - week_start).days


def build_profile_payload(profile: SyntheticProfile, week_start: date, week_index: int) -> dict[str, str | Decimal]:
    payload = initialize_line_fields()

    if profile.code == "REG":
        apply_regular_week(payload)
        return payload

    if profile.code == "EXT":
        set_day(payload, 0, REST_SHIFT)
        set_day(payload, 1, MORNING_SHIFT, AFTERNOON_SHIFT_LONG)
        set_day(payload, 2, DAY_SHIFT)
        set_day(payload, 3, MORNING_SHIFT, AFTERNOON_SHIFT_LONG)
        set_day(payload, 4, DAY_SHIFT)
        set_day(payload, 5, MORNING_SHIFT, AFTERNOON_SHIFT_LONG)
        set_day(payload, 6, DAY_SHIFT)
        return payload

    if profile.code == "NOC":
        set_day(payload, 0, REST_SHIFT)
        for day_index in range(1, 6):
            set_day(payload, day_index, EVENING_SHIFT)
        set_day(payload, 6, NIGHT_SHIFT)
        return payload

    if profile.code == "PDI":
        apply_regular_week(payload)
        if week_index in {0, 4}:
            set_day(payload, 0, DAY_SHIFT)
            payload["pending_days"] = Decimal("1.00")
            reference_date = date(2026, 4, 2) if week_index == 0 else date(2026, 5, 1)
            payload["pending_dates_note"] = reference_date.isoformat()
            return payload

        if week_index in {1, 5}:
            set_day(
                payload,
                0,
                REST_SHIFT,
                compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            )
            return payload

        return payload

    if profile.code == "PHR":
        apply_regular_week(payload)
        if week_index == 1:
            payload["pending_hours"] = Decimal("4.00")
            return payload
        if week_index == 2:
            set_day(
                payload,
                1,
                DAY_SHIFT_SHORT,
                compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                compensation_hours="2.00",
            )
            return payload
        if week_index == 3:
            set_day(
                payload,
                3,
                DAY_SHIFT_SHORT,
                compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                compensation_hours="2.00",
            )
            return payload
        if week_index == 6:
            payload["pending_hours"] = Decimal("3.50")
            return payload
        if week_index == 7:
            set_day(
                payload,
                2,
                DAY_SHIFT_HALF,
                compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                compensation_hours="1.50",
            )
            return payload
        if week_index == 8:
            set_day(
                payload,
                4,
                DAY_SHIFT_SHORT,
                compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                compensation_hours="2.00",
            )
            return payload
        return payload

    if profile.code == "INC":
        set_day(payload, 0, REST_SHIFT)
        set_day(payload, 1, DAY_SHIFT)
        set_day(payload, 2, SICK_SHIFT)
        set_day(payload, 3, SICK_SHIFT)
        set_day(payload, 4, DAY_SHIFT)
        set_day(payload, 5, DAY_SHIFT)
        set_day(payload, 6, DAY_SHIFT_TWO)
        if week_index in {3, 7}:
            set_day(payload, 1, SICK_SHIFT)
            set_day(payload, 4, SICK_SHIFT)
        return payload

    if profile.code == "INA":
        apply_regular_week(payload)
        set_day(payload, 3, ABSENCE_SHIFT)
        holiday_day_index = find_weekday_index_for_date(week_start, date(2026, 5, 1))
        if holiday_day_index is not None:
            set_day(payload, holiday_day_index, HOLIDAY_SHIFT)
        return payload

    if profile.code == "REN":
        if week_index < 7:
            apply_regular_week(payload)
            return payload
        set_day(payload, 0, REST_SHIFT)
        set_day(payload, 1, DAY_SHIFT)
        set_day(payload, 2, DAY_SHIFT)
        set_day(payload, 3, RESIGNATION_SHIFT)
        set_day(payload, 4, RESIGNATION_SHIFT)
        set_day(payload, 5, RESIGNATION_SHIFT)
        set_day(payload, 6, RESIGNATION_SHIFT)
        return payload

    return payload


def is_profile_active(profile: SyntheticProfile, week_index: int) -> bool:
    if profile.code == "REN" and week_index > 7:
        return False
    return True


class Command(BaseCommand):
    help = "Genera datos sinteticos de horarios para analitica entre abril y mayo de 2026."

    def add_arguments(self, parser):
        parser.add_argument("--start", default="2026-04-01", help="Fecha inicial del rango a cubrir.")
        parser.add_argument("--end", default="2026-05-31", help="Fecha final del rango a cubrir.")
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Reemplaza solo las lineas sinteticas previas (SIM...) en el rango indicado.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        start_date = date.fromisoformat(options["start"])
        end_date = date.fromisoformat(options["end"])
        replace = options["replace"]
        config = SystemConfiguration.load()
        week_start_dates: list[date] = []

        current_week_start = align_to_week_start(start_date, config.week_start_day)
        while current_week_start <= end_date:
            week_start_dates.append(current_week_start)
            current_week_start += timedelta(days=7)

        self._ensure_required_shift_templates()

        active_sites = list(Site.objects.filter(is_active=True).order_by("code"))
        active_roles = list(JobRole.objects.filter(is_active=True).order_by("name"))
        active_departments = list(Department.objects.filter(is_active=True).order_by("name"))
        owner_user = User.objects.filter(is_superuser=True).order_by("id").first()

        schedules_created = 0
        schedules_updated = 0
        lines_created = 0
        lines_updated = 0
        lines_deleted = 0

        if not active_sites:
            self.stdout.write(self.style.WARNING("No hay sedes activas para generar la carga sintetica."))
            return

        if not active_roles:
            self.stdout.write(self.style.WARNING("No hay cargos activos disponibles para asignar a la carga sintetica."))
            return

        for site_index, site in enumerate(active_sites):
            for week_index, week_start in enumerate(week_start_dates):
                status = self._status_for_week(week_start)
                schedule, created = WeeklySchedule.objects.get_or_create(
                    site=site,
                    week_start_date=week_start,
                    defaults={
                        "first_day_index": config.week_start_day,
                        "status": status,
                        "notes": self._build_schedule_note(),
                        "created_by": owner_user,
                        "updated_by": owner_user,
                    },
                )

                if created:
                    schedules_created += 1
                else:
                    schedules_updated += 1
                    if schedule.created_by_id is None:
                        schedule.created_by = owner_user
                    schedule.first_day_index = config.week_start_day
                    schedule.status = status
                    schedule.notes = self._build_schedule_note()
                    schedule.updated_by = owner_user
                    schedule.save()

                synthetic_ids_for_week: list[str] = []
                for profile_index, profile in enumerate(PROFILES):
                    if not is_profile_active(profile, week_index):
                        continue

                    employee_identifier = f"SIM{site.code}{profile.code}"
                    synthetic_ids_for_week.append(employee_identifier)
                    line, line_created = ScheduleLine.objects.get_or_create(
                        schedule=schedule,
                        employee_identifier=employee_identifier,
                    )
                    if line_created:
                        lines_created += 1
                    else:
                        lines_updated += 1

                    role = active_roles[(site_index + profile_index) % len(active_roles)]
                    department = (
                        active_departments[(site_index + profile_index) % len(active_departments)]
                        if active_departments else None
                    )
                    payload = build_profile_payload(profile, week_start, week_index)

                    line.employee_document_type = ""
                    line.employee_name = f"SIM {profile.label} {site.code}"
                    line.department_code = (department.code or "").strip() if department else ""
                    line.department_name = department.name if department else ""
                    line.job_role_code = (role.code or "").strip()
                    line.job_role_name = role.name
                    line.weekly_target_hours = role.weekly_target_hours or config.default_weekly_hours
                    line.daily_max_hours = role.daily_max_hours or config.default_daily_max_hours

                    for field_name, value in payload.items():
                        setattr(line, field_name, value)

                    save_schedule_line_with_balances(line)

                if replace:
                    deleted_count, _ = (
                        schedule.lines
                        .filter(employee_identifier__startswith="SIM")
                        .exclude(employee_identifier__in=synthetic_ids_for_week)
                        .delete()
                    )
                    lines_deleted += deleted_count

        self.stdout.write(
            self.style.SUCCESS(
                "Carga sintetica generada. "
                f"Horarios nuevos: {schedules_created}. "
                f"Horarios actualizados: {schedules_updated}. "
                f"Lineas nuevas: {lines_created}. "
                f"Lineas actualizadas: {lines_updated}. "
                f"Lineas sinteticas eliminadas: {lines_deleted}."
            )
        )

    def _ensure_required_shift_templates(self) -> None:
        ensure_shift_template(
            label=DAY_SHIFT,
            start_time=parse_time("08:00"),
            end_time=parse_time("16:00"),
            duration_hours="8.00",
            display_order=9100,
        )
        ensure_shift_template(
            label=DAY_SHIFT_SHORT,
            start_time=parse_time("06:00"),
            end_time=parse_time("12:00"),
            duration_hours="6.00",
            display_order=9101,
        )
        ensure_shift_template(
            label=DAY_SHIFT_HALF,
            start_time=parse_time("06:30"),
            end_time=parse_time("13:00"),
            duration_hours="6.50",
            display_order=9102,
        )
        ensure_shift_template(
            label=DAY_SHIFT_TWO,
            start_time=parse_time("06:00"),
            end_time=parse_time("08:00"),
            duration_hours="2.00",
            display_order=9103,
        )
        ensure_shift_template(
            label=MORNING_SHIFT,
            start_time=parse_time("06:00"),
            end_time=parse_time("10:00"),
            duration_hours="4.00",
            display_order=9104,
        )
        ensure_shift_template(
            label=AFTERNOON_SHIFT,
            start_time=parse_time("13:00"),
            end_time=parse_time("17:00"),
            duration_hours="4.00",
            display_order=9105,
        )
        ensure_shift_template(
            label=AFTERNOON_SHIFT_LONG,
            start_time=parse_time("13:00"),
            end_time=parse_time("18:00"),
            duration_hours="5.00",
            display_order=9106,
        )
        ensure_shift_template(
            label=EVENING_SHIFT,
            start_time=parse_time("14:00"),
            end_time=parse_time("22:00"),
            duration_hours="8.00",
            night_bonus_hours="3.00",
            display_order=9107,
        )
        ensure_shift_template(
            label=NIGHT_SHIFT,
            start_time=parse_time("18:00"),
            end_time=parse_time("00:00"),
            duration_hours="6.00",
            night_bonus_hours="5.00",
            display_order=9108,
        )
        ensure_shift_template(
            label=REST_SHIFT,
            start_time=None,
            end_time=None,
            duration_hours="0.00",
            counts_as_worked_time=False,
            display_order=9190,
        )
        ensure_shift_template(
            label=SICK_SHIFT,
            start_time=None,
            end_time=None,
            duration_hours="0.00",
            counts_as_worked_time=False,
            display_order=9191,
        )
        ensure_shift_template(
            label=ABSENCE_SHIFT,
            start_time=None,
            end_time=None,
            duration_hours="0.00",
            counts_as_worked_time=False,
            display_order=9192,
        )
        ensure_shift_template(
            label=RESIGNATION_SHIFT,
            start_time=None,
            end_time=None,
            duration_hours="0.00",
            counts_as_worked_time=False,
            display_order=9193,
        )
        ensure_shift_template(
            label=HOLIDAY_SHIFT,
            start_time=None,
            end_time=None,
            duration_hours="0.00",
            counts_as_worked_time=False,
            display_order=9194,
        )

    def _status_for_week(self, week_start: date) -> str:
        if week_start <= date(2026, 4, 26):
            return WeeklySchedule.Status.PUBLISHED
        if week_start <= date(2026, 5, 10):
            return WeeklySchedule.Status.REVIEW
        return WeeklySchedule.Status.DRAFT

    def _build_schedule_note(self) -> str:
        return (
            "[SIMULADO] Carga analitica generica para abril y mayo de 2026. "
            "Incluye horas extra, recargo nocturno, pendientes por descanso/festivo, "
            "incapacidades, inasistencias, renuncias, pago dia y pago horas."
        )
