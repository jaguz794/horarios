from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from core.access import get_accessible_schedules_queryset, get_accessible_sites_queryset
from core.models import JobRole, ShiftTemplate, Site, SystemConfiguration
from legacy.services import lookup_third_party_by_identifier
from schedules.calendar_utils import get_special_day_label
from schedules.models import ScheduleLine, WeeklySchedule
from schedules.services import (
    get_active_overtime_restriction,
    is_employee_blacklisted,
    get_daily_overtime_hours,
    get_rest_shift_label,
    get_schedule_line_balance_snapshot,
    get_schedule_line_compact_alert_summary,
    resolve_compensation_usage,
    resolve_shift_metrics,
)

NOON_TIME = datetime.strptime("12:00", "%H:%M").time()
DOCUMENT_NUMBER_PATTERN = re.compile(r"^[0-9A-Za-z-]{3,30}$")


def current_week_start() -> timezone.datetime.date:
    today = timezone.localdate()
    config = SystemConfiguration.load()
    offset = (today.weekday() - config.week_start_day) % 7
    return today - timedelta(days=offset)


def build_shift_choices(second_slot: bool = False) -> list[tuple[str, str] | tuple[str, list[tuple[str, str]]]]:
    blank_label = "Sin segundo turno" if second_slot else "Sin turno"
    grouped_choices: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    existing_labels: set[str] = set()

    for shift in ShiftTemplate.objects.filter(is_active=True).order_by("display_order", "label"):
        if second_slot and not shift.can_be_second_shift:
            continue
        grouped_choices[shift.choice_group].append((shift.label, shift.label))
        existing_labels.add(shift.label.casefold())

    if not second_slot and "descanso" not in existing_labels:
        grouped_choices["Novedades"].append(("descanso", "descanso"))

    choices: list[tuple[str, str] | tuple[str, list[tuple[str, str]]]] = [("", blank_label)]
    choices.extend((group_name, options) for group_name, options in grouped_choices.items())
    return choices


class DatePickerInput(forms.DateInput):
    input_type = "date"


class TrimmedNumberInput(forms.NumberInput):
    def format_value(self, value):
        if value in (None, ""):
            return None
        try:
            decimal_value = Decimal(str(value)).normalize()
            rendered = format(decimal_value, "f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            return rendered or "0"
        except (InvalidOperation, TypeError, ValueError):
            return super().format_value(value)


class IntegerNumberInput(TrimmedNumberInput):
    def format_value(self, value):
        if value in (None, ""):
            return None
        try:
            return str(int(Decimal(str(value))))
        except (InvalidOperation, TypeError, ValueError):
            return super().format_value(value)


class StyledFormMixin:
    field_class = "input"

    def apply_style(self):
        for field in self.fields.values():
            css_class = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css_class} {self.field_class}".strip()


class ScheduleFilterForm(StyledFormMixin, forms.Form):
    site = forms.ModelChoiceField(queryset=Site.objects.none(), required=False, label="Sede")
    status = forms.ChoiceField(
        required=False,
        label="Estado",
        choices=[("", "Todos")] + list(WeeklySchedule.Status.choices),
    )
    week_start_date = forms.DateField(
        required=False,
        label="Fecha inicio de semana",
        widget=DatePickerInput(),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = get_accessible_sites_queryset(user, Site.objects.filter(is_active=True))
        self.fields["site"].empty_label = "Todas"
        self.apply_style()


class ScheduleLoadForm(StyledFormMixin, forms.Form):
    site = forms.ModelChoiceField(
        queryset=Site.objects.none(),
        label="Sede",
    )
    week_start_date = forms.DateField(
        label="Dia inicio de semana",
        widget=DatePickerInput(attrs={"step": "7", "min": "2024-01-07"}),
        initial=current_week_start,
        help_text="Ingresa el domingo inicial de la semana que vas a programar.",
    )
    copy_from_schedule = forms.ModelChoiceField(
        queryset=WeeklySchedule.objects.none(),
        required=False,
        label="Copiar turnos desde",
        help_text="Opcional. Trae los turnos de una semana anterior de la misma sede para usarlos como base.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = get_accessible_sites_queryset(user, Site.objects.filter(is_active=True))
        copy_queryset = get_accessible_schedules_queryset(
            user,
            WeeklySchedule.objects.select_related("site"),
        ).order_by("-week_start_date", "site__code")
        self.fields["copy_from_schedule"].queryset = copy_queryset
        self.fields["copy_from_schedule"].empty_label = "No copiar una semana anterior"
        self.fields["copy_from_schedule"].label_from_instance = (
            lambda obj: f"{obj.site.name} | {obj.week_start_date:%d/%m/%Y} a {obj.week_end_date:%d/%m/%Y}"
        )
        self.apply_style()

    def clean_week_start_date(self):
        week_start_date = self.cleaned_data["week_start_date"]
        if week_start_date.weekday() != 6:
            raise forms.ValidationError("El horario solo se puede crear usando domingos como inicio de semana.")
        return week_start_date

    def clean(self):
        cleaned_data = super().clean()
        site = cleaned_data.get("site")
        week_start_date = cleaned_data.get("week_start_date")
        copy_from_schedule = cleaned_data.get("copy_from_schedule")

        if copy_from_schedule and site and copy_from_schedule.site_id != site.pk:
            self.add_error("copy_from_schedule", "La plantilla a copiar debe corresponder a la misma sede.")

        if copy_from_schedule and site and week_start_date:
            if copy_from_schedule.site_id == site.pk and copy_from_schedule.week_start_date == week_start_date:
                self.add_error("copy_from_schedule", "Selecciona una semana diferente a la que vas a crear.")

        return cleaned_data


class WeeklyScheduleForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = WeeklySchedule
        fields = ["status", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, readonly: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_style()
        if readonly:
            for field in self.fields.values():
                field.disabled = True


class ScheduleLineManualAddForm(StyledFormMixin, forms.Form):
    employee_identifier = forms.CharField(
        label="Numero documento",
        max_length=30,
        widget=forms.TextInput(attrs={"placeholder": "Ej. 1000123456"}),
    )
    lookup_attempts = forms.IntegerField(widget=forms.HiddenInput(), required=False, initial=0)
    employee_name = forms.CharField(
        label="Nombre completo",
        max_length=180,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Se llenara automaticamente desde terceros"}),
    )
    job_role = forms.ModelChoiceField(
        queryset=JobRole.objects.none(),
        label="Cargo",
        empty_label="Selecciona un cargo",
    )

    def __init__(
        self,
        *args,
        schedule=None,
        readonly: bool = False,
        manual_name_enabled: bool = False,
        lookup_found: bool = False,
        **kwargs,
    ):
        self.schedule = schedule
        self.manual_name_enabled = manual_name_enabled
        self.lookup_found = lookup_found
        self.lookup_result = None
        super().__init__(*args, **kwargs)
        self.fields["job_role"].queryset = JobRole.objects.filter(is_active=True).order_by("name")
        self.apply_style()

        if not manual_name_enabled:
            self.fields["employee_name"].widget.attrs["readonly"] = True
            if lookup_found:
                self.fields["employee_name"].widget.attrs["placeholder"] = "Nombre encontrado en el sistema"

        if readonly:
            for field in self.fields.values():
                field.disabled = True

    def clean_employee_identifier(self):
        value = (self.cleaned_data.get("employee_identifier") or "").strip().upper()
        if not DOCUMENT_NUMBER_PATTERN.fullmatch(value):
            raise forms.ValidationError(
                "El numero de documento solo puede contener letras, numeros o guiones, y debe tener entre 3 y 30 caracteres."
            )
        if self.schedule and ScheduleLine.objects.filter(
            schedule=self.schedule,
            employee_identifier=value,
        ).exists():
            raise forms.ValidationError("Ese numero de documento ya existe en este horario.")
        if is_employee_blacklisted(value):
            raise forms.ValidationError(
                "Ese numero de documento esta bloqueado en la lista negra y no se puede cargar en horarios."
            )
        return value

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data

        employee_identifier = cleaned_data.get("employee_identifier", "")
        lookup_attempts = int(cleaned_data.get("lookup_attempts") or 0)
        lookup_result = lookup_third_party_by_identifier(employee_identifier)
        self.lookup_result = lookup_result

        if lookup_result:
            cleaned_data["employee_name"] = lookup_result.employee_name
            return cleaned_data

        if lookup_attempts < 2:
            raise forms.ValidationError(
                "La persona no esta creada en el sistema. Vuelve a consultar para habilitar el cargue manual."
            )

        employee_name = " ".join((cleaned_data.get("employee_name") or "").split())
        if len(employee_name) < 3:
            self.add_error("employee_name", "Ingresa un nombre valido para el trabajador.")
        cleaned_data["employee_name"] = employee_name
        return cleaned_data

    def save(self) -> ScheduleLine:
        config = SystemConfiguration.load()
        job_role = self.cleaned_data["job_role"]
        line = ScheduleLine(
            schedule=self.schedule,
            employee_document_type="",
            employee_identifier=self.cleaned_data["employee_identifier"],
            employee_name=self.cleaned_data["employee_name"],
            department_code="",
            department_name="",
            job_role_code=(job_role.code or "").strip(),
            job_role_name=job_role.name,
            weekly_target_hours=job_role.weekly_target_hours or config.default_weekly_hours,
            daily_max_hours=job_role.daily_max_hours or config.default_daily_max_hours,
        )
        return line


class ScheduleLineForm(StyledFormMixin, forms.ModelForm):
    SHIFT_1_FIELDS = [f"day_{index}_shift_1" for index in range(7)]
    SHIFT_2_FIELDS = [f"day_{index}_shift_2" for index in range(7)]
    COMPENSATION_MODE_FIELDS = [f"day_{index}_compensation_mode" for index in range(7)]
    COMPENSATION_HOURS_FIELDS = [f"day_{index}_compensation_hours" for index in range(7)]
    manual_day_adjustment = forms.IntegerField(
        required=False,
        widget=IntegerNumberInput(attrs={"step": "1", "class": "input input--numeric input--days"}),
    )
    manual_hour_adjustment = forms.DecimalField(
        required=False,
        decimal_places=2,
        widget=TrimmedNumberInput(attrs={"step": "0.50", "class": "input input--numeric input--hours"}),
    )

    class Meta:
        model = ScheduleLine
        fields = [
            "day_0_shift_1",
            "day_0_shift_2",
            "day_0_compensation_mode",
            "day_0_compensation_hours",
            "day_1_shift_1",
            "day_1_shift_2",
            "day_1_compensation_mode",
            "day_1_compensation_hours",
            "day_2_shift_1",
            "day_2_shift_2",
            "day_2_compensation_mode",
            "day_2_compensation_hours",
            "day_3_shift_1",
            "day_3_shift_2",
            "day_3_compensation_mode",
            "day_3_compensation_hours",
            "day_4_shift_1",
            "day_4_shift_2",
            "day_4_compensation_mode",
            "day_4_compensation_hours",
            "day_5_shift_1",
            "day_5_shift_2",
            "day_5_compensation_mode",
            "day_5_compensation_hours",
            "day_6_shift_1",
            "day_6_shift_2",
            "day_6_compensation_mode",
            "day_6_compensation_hours",
            "manual_day_adjustment",
            "manual_hour_adjustment",
        ]

    def __init__(
        self,
        *args,
        schedule=None,
        shift_choices=None,
        secondary_shift_choices=None,
        readonly: bool = False,
        allow_money_payment: bool = False,
        show_admin_fields: bool = False,
        **kwargs,
    ):
        self.schedule = schedule
        self.allow_money_payment = allow_money_payment
        self.show_admin_fields = show_admin_fields
        super().__init__(*args, **kwargs)
        if self.schedule is not None and getattr(self.instance, "schedule_id", None) is None:
            self.instance.schedule = self.schedule
        self.overtime_restriction = get_active_overtime_restriction(self.instance.employee_identifier)
        self.overtime_restriction_daily_limit = (
            Decimal(str(self.overtime_restriction.max_daily_overtime_hours))
            if self.overtime_restriction is not None
            else None
        )
        self.overtime_restriction_limit = (
            Decimal(str(self.overtime_restriction.max_weekly_overtime_hours))
            if self.overtime_restriction is not None
            else None
        )

        shift_1_choices = shift_choices or build_shift_choices(second_slot=False)
        shift_2_choices = secondary_shift_choices or build_shift_choices(second_slot=True)
        payment_choices = [
            ScheduleLine.CompensationMode.NONE,
            ScheduleLine.CompensationMode.PAY_DAY,
            ScheduleLine.CompensationMode.PAY_HOURS,
        ]
        if self.allow_money_payment:
            payment_choices.append(ScheduleLine.CompensationMode.PAY_MONEY)
        payment_choices_render = [(value, label) for value, label in ScheduleLine.CompensationMode.choices if value in payment_choices]

        for field_name in self.SHIFT_1_FIELDS:
            self.fields[field_name].widget = forms.Select(
                choices=shift_1_choices,
                attrs={"class": "input input--compact"},
            )

        for field_name in self.SHIFT_2_FIELDS:
            self.fields[field_name].widget = forms.Select(
                choices=shift_2_choices,
                attrs={"class": "input input--compact"},
            )

        for field_name in self.COMPENSATION_MODE_FIELDS:
            self.fields[field_name].choices = payment_choices_render
            self.fields[field_name].widget.attrs["class"] = "input input--compact input--payment-mode"

        for field_name in self.COMPENSATION_HOURS_FIELDS:
            self.fields[field_name].required = False
            self.fields[field_name].widget = TrimmedNumberInput(
                attrs={
                    "step": "0.50",
                    "placeholder": "Horas",
                    "class": "input input--compact input--numeric input--pay-hours",
                }
            )

        self.balance_snapshot = get_schedule_line_balance_snapshot(self.instance)
        self.compact_alert_summary = get_schedule_line_compact_alert_summary(
            self.instance,
            balance_snapshot=self.balance_snapshot,
        )
        self.apply_style()

        if not self.show_admin_fields:
            self.fields["manual_day_adjustment"].widget = forms.HiddenInput()
            self.fields["manual_day_adjustment"].disabled = True
            self.fields["manual_hour_adjustment"].widget = forms.HiddenInput()
            self.fields["manual_hour_adjustment"].disabled = True

        if readonly:
            for field in self.fields.values():
                field.disabled = True

    def clean(self):
        cleaned_data = super().clean()
        config = SystemConfiguration.load()
        day_reference_hours = self.balance_snapshot["day_reference_hours"]
        rest_shift_label = get_rest_shift_label()
        shift_labels = {
            cleaned_data.get(field_name)
            for field_name in self.SHIFT_1_FIELDS + self.SHIFT_2_FIELDS
            if cleaned_data.get(field_name)
        }
        shift_map = {
            shift.label: shift
            for shift in ShiftTemplate.objects.filter(label__in=shift_labels)
        }
        prior_day_balance = max(self.balance_snapshot["prior_day_balance"], Decimal("0.00"))
        prior_hour_balance = max(self.balance_snapshot["prior_hour_balance"], Decimal("0.00"))
        compensation_entries: list[dict[str, Decimal | int | str]] = []
        total_worked_hours = Decimal("0.00")
        weekly_target_hours = Decimal(str(self.instance.weekly_target_hours or config.default_weekly_hours or "0"))

        for index in range(7):
            shift_1_field = f"day_{index}_shift_1"
            shift_2_field = f"day_{index}_shift_2"
            compensation_mode_field = f"day_{index}_compensation_mode"
            compensation_hours_field = f"day_{index}_compensation_hours"
            compensation_mode = cleaned_data.get(compensation_mode_field, "") or ""
            compensation_hours = Decimal(str(cleaned_data.get(compensation_hours_field) or "0"))
            compensation_entries.append(
                {
                    "index": index,
                    "mode": compensation_mode,
                    "hours": compensation_hours,
                    "worked_hours": Decimal("0.00"),
                    "special_generated": False,
                }
            )

            if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
                cleaned_data[shift_1_field] = rest_shift_label
                cleaned_data[shift_2_field] = ""
                cleaned_data[compensation_hours_field] = Decimal("0.00")
            elif compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
                if compensation_hours <= Decimal("0.00"):
                    self.add_error(compensation_hours_field, "Pago horas requiere una cantidad mayor que cero.")
            elif compensation_mode == ScheduleLine.CompensationMode.PAY_MONEY:
                if not self.allow_money_payment:
                    self.add_error(compensation_mode_field, "Pago en dinero solo esta disponible para el perfil administrador.")
                if compensation_hours <= Decimal("0.00"):
                    self.add_error(compensation_hours_field, "Pago en dinero requiere una cantidad mayor que cero.")
            else:
                cleaned_data[compensation_hours_field] = Decimal("0.00")

            shift_1_label = cleaned_data.get(shift_1_field, "")
            shift_2_label = cleaned_data.get(shift_2_field, "")
            shift_1 = shift_map.get(shift_1_label)
            shift_2 = shift_map.get(shift_2_label)
            shift_1_hours, _ = resolve_shift_metrics(shift_1_label, config=config, shift_templates=shift_map)
            shift_2_hours, _ = resolve_shift_metrics(shift_2_label, config=config, shift_templates=shift_map)
            day_worked_hours = shift_1_hours + shift_2_hours
            total_worked_hours += day_worked_hours
            compensation_entries[-1]["worked_hours"] = day_worked_hours
            if (
                self.overtime_restriction is not None
                and self.overtime_restriction_daily_limit is not None
            ):
                daily_overtime_hours = get_daily_overtime_hours(day_worked_hours, day_reference_hours)
                if daily_overtime_hours > self.overtime_restriction_daily_limit:
                    target_field = shift_2_field if shift_2_label else shift_1_field
                    self.add_error(
                        target_field,
                        "Restriccion medica: no puede superar "
                        f"{self.overtime_restriction_daily_limit} h extra en el dia. "
                        f"Esta programacion genera {daily_overtime_hours} h extra.",
                    )
            schedule_for_day = self.instance.schedule if getattr(self.instance, "schedule_id", None) else self.schedule
            is_special_day = bool(
                schedule_for_day
                and schedule_for_day.week_start_date
                and get_special_day_label(schedule_for_day.week_start_date + timedelta(days=index))
            )
            compensation_entries[-1]["special_generated"] = bool(day_worked_hours > Decimal("0.00") and is_special_day)

            if shift_2 and not shift_1:
                self.add_error(shift_1_field, "Debes seleccionar primero el turno 1.")
                self.add_error(shift_2_field, "El turno 2 no puede quedar solo.")
                continue

            if compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
                compensated_day_hours = day_worked_hours + compensation_hours
                if day_worked_hours >= day_reference_hours:
                    self.add_error(
                        compensation_hours_field,
                        "Ese dia ya cumple la jornada con las horas laboradas; no requiere pago por horas.",
                    )
                elif compensated_day_hours > day_reference_hours:
                    self.add_error(
                        compensation_hours_field,
                        "El pago por horas supera las horas necesarias para completar la jornada del dia.",
                    )
            elif (
                compensation_mode == ScheduleLine.CompensationMode.PAY_MONEY
                and compensation_hours > day_reference_hours
            ):
                self.add_error(
                    compensation_hours_field,
                    "El pago en dinero no puede descontar mas de la jornada diaria en un mismo dia.",
                )

            if not shift_1 or not shift_2:
                continue

            if not shift_1.counts_as_worked_time:
                self.add_error(shift_2_field, "Si el primer valor es una novedad, el turno 2 debe quedar vacio.")
                continue

            if not shift_2.counts_as_worked_time:
                self.add_error(shift_2_field, "Las novedades van solo en el turno 1.")

            if shift_1.is_full_day_shift:
                self.add_error(shift_2_field, "Las jornadas continuas van solo en la casilla del turno 1.")

            if shift_2_label and shift_1_hours >= day_reference_hours:
                self.add_error(
                    shift_2_field,
                    "Si el turno 1 ya cumple la jornada del dia, el turno 2 debe quedar vacio.",
                )

            if shift_1.start_time and shift_1.start_time >= NOON_TIME and not shift_1.is_full_day_shift:
                self.add_error(
                    shift_1_field,
                    "Cuando hay turno 2, el turno 1 debe ser el primer tramo del dia.",
                )

            if shift_2.start_time and shift_2.start_time < NOON_TIME and not shift_2.spans_next_day:
                self.add_error(
                    shift_2_field,
                    "El turno 2 debe corresponder a la tarde o la noche.",
                )

            if shift_1.start_time and shift_1.end_time and shift_2.start_time:
                first_end = datetime.combine(date.today(), shift_1.end_time)
                if shift_1.spans_next_day:
                    first_end += timedelta(days=1)

                second_start = datetime.combine(date.today(), shift_2.start_time)
                if shift_2.spans_next_day and shift_2.start_time < shift_1.start_time:
                    second_start += timedelta(days=1)

                if second_start < first_end:
                    self.add_error(
                        shift_2_field,
                        "El turno 2 debe iniciar despues de que termine el turno 1.",
                    )

        manual_day_adjustment = Decimal(str(cleaned_data.get("manual_day_adjustment") or "0"))
        manual_hour_adjustment = Decimal(str(cleaned_data.get("manual_hour_adjustment") or "0"))
        payment_resolution = resolve_compensation_usage(
            compensation_entries,
            available_day_balance=prior_day_balance + manual_day_adjustment,
            available_hour_balance=prior_hour_balance + manual_hour_adjustment,
            day_reference_hours=day_reference_hours,
            weekly_target_hours=self.instance.weekly_target_hours or config.default_weekly_hours,
        )

        for index in payment_resolution["invalid_pay_day_indices"]:
            self.add_error(
                f"day_{index}_compensation_mode",
                f"Pago dia requiere 1 dia acumulado o {day_reference_hours} h acumuladas disponibles hasta ese dia.",
            )

        for index in payment_resolution["invalid_pay_hours_indices"]:
            compensation_hours = Decimal(str(cleaned_data.get(f"day_{index}_compensation_hours") or "0"))
            if compensation_hours > Decimal("0.00"):
                self.add_error(
                    f"day_{index}_compensation_hours",
                    "Las horas a descontar superan el saldo acumulado disponible hasta ese dia.",
                )

        for index in payment_resolution["invalid_pay_money_indices"]:
            compensation_hours = Decimal(str(cleaned_data.get(f"day_{index}_compensation_hours") or "0"))
            if compensation_hours > Decimal("0.00"):
                self.add_error(
                    f"day_{index}_compensation_hours",
                    "El pago en dinero supera el saldo acumulado disponible hasta ese dia.",
                )

        weekly_overtime_hours = max(total_worked_hours - weekly_target_hours, Decimal("0.00"))
        if (
            self.overtime_restriction is not None
            and self.overtime_restriction_limit is not None
            and weekly_overtime_hours > self.overtime_restriction_limit
        ):
            employee_name = self.instance.employee_name or "Este trabajador"
            self.add_error(
                None,
                f"{employee_name} tiene restriccion medica y no puede superar "
                f"{self.overtime_restriction_limit} h extra en la semana. "
                f"La programacion actual genera {weekly_overtime_hours} h extra.",
            )

        return cleaned_data


ScheduleLineFormSet = inlineformset_factory(
    WeeklySchedule,
    ScheduleLine,
    form=ScheduleLineForm,
    extra=0,
    can_delete=False,
)


class ReportRangeForm(StyledFormMixin, forms.Form):
    site = forms.ModelChoiceField(queryset=Site.objects.none(), required=False, label="Sede")
    date_from = forms.DateField(label="Desde", widget=DatePickerInput())
    date_to = forms.DateField(label="Hasta", widget=DatePickerInput())

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = get_accessible_sites_queryset(user, Site.objects.filter(is_active=True))
        self.fields["site"].empty_label = "Todas"
        self.apply_style()

    def clean(self):
        cleaned_data = super().clean()
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            self.add_error("date_to", "La fecha final debe ser mayor o igual a la fecha inicial.")
        return cleaned_data


class WeeklyBalanceReportForm(StyledFormMixin, forms.Form):
    site = forms.ModelChoiceField(queryset=Site.objects.none(), required=False, label="Sede")
    week_start_date = forms.DateField(
        label="Semana",
        widget=DatePickerInput(),
        initial=current_week_start,
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = get_accessible_sites_queryset(user, Site.objects.filter(is_active=True))
        self.fields["site"].empty_label = "Todas"
        self.apply_style()


class ScheduleSettlementForm(StyledFormMixin, forms.Form):
    site = forms.ModelChoiceField(queryset=Site.objects.none(), required=False, label="Sede")
    week_start_date = forms.DateField(
        label="Semana",
        widget=DatePickerInput(),
        initial=current_week_start,
        help_text="Selecciona el primer dia de la semana del horario publicado.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = get_accessible_sites_queryset(user, Site.objects.filter(is_active=True))
        self.fields["site"].empty_label = "Todas"
        self.apply_style()


class InitialBalanceUploadForm(StyledFormMixin, forms.Form):
    file = forms.FileField(
        label="Archivo Excel o CSV",
        help_text=(
            "Carga un archivo .xlsx o .csv con columnas como Cedula, Nombres y apellidos, "
            "Dias extras y Horas extras."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_style()

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        file_name = (uploaded_file.name or "").strip().lower()
        if not file_name.endswith((".xlsx", ".csv")):
            raise forms.ValidationError("El cargador de saldos iniciales solo admite archivos .xlsx o .csv.")
        return uploaded_file
