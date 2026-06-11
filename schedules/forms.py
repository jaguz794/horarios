from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from core.access import get_accessible_sites_queryset
from core.models import JobRole, ShiftTemplate, Site, SystemConfiguration
from schedules.models import ScheduleLine, WeeklySchedule
from schedules.services import (
    get_schedule_line_balance_snapshot,
    get_schedule_line_compact_alert_summary,
    get_rest_shift_label,
    recalculate_schedule_line,
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


def parse_multiple_dates(serialized_value: str) -> list[date]:
    if not serialized_value:
        return []

    values: list[date] = []
    for raw_value in serialized_value.split(","):
        cleaned = raw_value.strip()
        if not cleaned:
            continue
        values.append(date.fromisoformat(cleaned))

    unique_values = sorted(set(values))
    return unique_values


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


class MultiDateWidget(forms.Widget):
    template_name = "widgets/multi_date_input.html"

    def format_value(self, value):
        if not value:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [str(item) for item in value]

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        normalized_values = self.format_value(value)
        serialized_value = ",".join(normalized_values)
        context["widget"]["value"] = serialized_value
        context["widget"]["date_values"] = normalized_values
        return context

    def value_from_datadict(self, data, files, name):
        return data.get(name, "")


class StyledFormMixin:
    field_class = "input"

    def apply_style(self):
        for field in self.fields.values():
            css_class = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css_class} {self.field_class}".strip()


class ScheduleLoadForm(StyledFormMixin, forms.Form):
    site = forms.ModelChoiceField(
        queryset=Site.objects.none(),
        label="Sede",
    )
    week_start_date = forms.DateField(
        label="Dia inicio de semana",
        widget=DatePickerInput(),
        initial=current_week_start,
        help_text="Ingresa el primer dia de la semana que vas a programar.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = get_accessible_sites_queryset(user, Site.objects.filter(is_active=True))
        self.apply_style()


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
    employee_name = forms.CharField(
        label="Nombre completo",
        max_length=180,
        widget=forms.TextInput(attrs={"placeholder": "Nombre del trabajador"}),
    )
    job_role = forms.ModelChoiceField(
        queryset=JobRole.objects.none(),
        label="Cargo",
        empty_label="Selecciona un cargo",
    )

    def __init__(self, *args, schedule=None, readonly: bool = False, **kwargs):
        self.schedule = schedule
        super().__init__(*args, **kwargs)
        self.fields["job_role"].queryset = JobRole.objects.filter(is_active=True).order_by("name")
        self.apply_style()
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
        return value

    def clean_employee_name(self):
        value = " ".join((self.cleaned_data.get("employee_name") or "").split())
        if len(value) < 3:
            raise forms.ValidationError("Ingresa un nombre valido para el trabajador.")
        return value

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
        recalculate_schedule_line(line)
        line.save()
        return line


class ScheduleLineForm(StyledFormMixin, forms.ModelForm):
    SHIFT_1_FIELDS = [f"day_{index}_shift_1" for index in range(7)]
    SHIFT_2_FIELDS = [f"day_{index}_shift_2" for index in range(7)]
    COMPENSATION_MODE_FIELDS = [f"day_{index}_compensation_mode" for index in range(7)]
    COMPENSATION_HOURS_FIELDS = [f"day_{index}_compensation_hours" for index in range(7)]
    pending_days = forms.IntegerField(
        min_value=0,
        widget=IntegerNumberInput(attrs={"step": "1", "min": "0", "class": "input input--numeric input--days"}),
    )
    pending_hours = forms.DecimalField(
        min_value=0,
        decimal_places=2,
        widget=TrimmedNumberInput(attrs={"step": "0.50", "min": "0", "class": "input input--numeric input--hours"}),
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
            "pending_dates_note",
            "pending_days",
            "pending_hours",
        ]
        widgets = {
            "pending_dates_note": MultiDateWidget(),
        }

    def __init__(
        self,
        *args,
        schedule=None,
        shift_choices=None,
        secondary_shift_choices=None,
        readonly: bool = False,
        **kwargs,
    ):
        self.schedule = schedule
        super().__init__(*args, **kwargs)
        if self.schedule is not None and getattr(self.instance, "schedule_id", None) is None:
            self.instance.schedule = self.schedule
        shift_1_choices = shift_choices or build_shift_choices(second_slot=False)
        shift_2_choices = secondary_shift_choices or build_shift_choices(second_slot=True)

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
            self.fields[field_name].widget.attrs["class"] = "input input--compact input--payment-mode"

        for field_name in self.COMPENSATION_HOURS_FIELDS:
            self.fields[field_name].required = False
            self.fields[field_name].widget = TrimmedNumberInput(
                attrs={
                    "step": "0.50",
                    "min": "0",
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
        if readonly:
            for field in self.fields.values():
                field.disabled = True

    def clean_pending_dates_note(self):
        raw_value = self.cleaned_data.get("pending_dates_note", "")
        try:
            parsed_dates = parse_multiple_dates(raw_value)
        except ValueError as exc:
            raise forms.ValidationError("Hay una fecha pendiente con formato invalido.") from exc

        return ",".join(item.isoformat() for item in parsed_dates)

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
        pending_dates = parse_multiple_dates(cleaned_data.get("pending_dates_note", ""))
        pending_days = cleaned_data.get("pending_days") or 0
        prior_day_balance = max(self.balance_snapshot["prior_day_balance"], Decimal("0.00"))
        prior_hour_balance = max(self.balance_snapshot["prior_hour_balance"], Decimal("0.00"))
        current_payment_days = 0
        current_payment_hours = Decimal("0.00")
        payment_day_fields: list[str] = []
        payment_hours_fields: list[str] = []

        if len(pending_dates) != pending_days:
            message = "La cantidad de fechas pendientes debe coincidir con dias pendientes."
            self.add_error("pending_dates_note", message)
            self.add_error("pending_days", message)

        for index in range(7):
            shift_1_field = f"day_{index}_shift_1"
            shift_2_field = f"day_{index}_shift_2"
            compensation_mode_field = f"day_{index}_compensation_mode"
            compensation_hours_field = f"day_{index}_compensation_hours"
            compensation_mode = cleaned_data.get(compensation_mode_field, "") or ""
            compensation_hours = Decimal(str(cleaned_data.get(compensation_hours_field) or "0"))

            if compensation_mode == ScheduleLine.CompensationMode.PAY_DAY:
                payment_day_fields.append(compensation_mode_field)
                current_payment_days += 1
                cleaned_data[shift_1_field] = rest_shift_label
                cleaned_data[shift_2_field] = ""
                cleaned_data[compensation_hours_field] = Decimal("0.00")
            elif compensation_mode == ScheduleLine.CompensationMode.PAY_HOURS:
                payment_hours_fields.append(compensation_hours_field)
                if compensation_hours <= Decimal("0.00"):
                    self.add_error(
                        compensation_hours_field,
                        "Pago horas requiere una cantidad mayor que cero.",
                    )
                else:
                    current_payment_hours += compensation_hours
            else:
                cleaned_data[compensation_hours_field] = Decimal("0.00")

            shift_1_label = cleaned_data.get(shift_1_field, "")
            shift_2_label = cleaned_data.get(shift_2_field, "")
            shift_1 = shift_map.get(shift_1_label)
            shift_2 = shift_map.get(shift_2_label)
            shift_1_hours, _ = resolve_shift_metrics(shift_1_label, config=config, shift_templates=shift_map)
            shift_2_hours, _ = resolve_shift_metrics(shift_2_label, config=config, shift_templates=shift_map)
            day_worked_hours = shift_1_hours + shift_2_hours

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

        if Decimal(str(current_payment_days)) > prior_day_balance:
            message = "No hay suficientes dias pendientes de horarios anteriores para aplicar pago dia."
            for field_name in payment_day_fields:
                self.add_error(field_name, message)

        if current_payment_hours > prior_hour_balance:
            message = "Las horas marcadas como pago superan el saldo previo disponible de horarios anteriores."
            for field_name in payment_hours_fields:
                self.add_error(field_name, message)

        return cleaned_data


ScheduleLineFormSet = inlineformset_factory(
    WeeklySchedule,
    ScheduleLine,
    form=ScheduleLineForm,
    extra=0,
    can_delete=False,
)
