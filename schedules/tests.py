from io import BytesIO, StringIO
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from core.models import Holiday, JobRole, OperationalStaffCache, ShiftTemplate, Site, SystemConfiguration, UserSiteAccess
from legacy.services import OperationalStaffingRecord
from schedules.forms import ScheduleLineForm, ScheduleLoadForm
from schedules.models import (
    EmployeeInitialBalance,
    EmployeeOvertimeRestriction,
    EmployeeScheduleBlacklist,
    ScheduleLine,
    WeeklySchedule,
)
from schedules.services import (
    build_schedule_balance_audit_rows,
    copy_schedule_template,
    get_rest_shift_label,
    get_schedule_line_compact_alert_summary,
    get_schedule_line_balance_snapshot,
    import_employee_initial_balances,
    parse_shift_hours,
    recalculate_schedule_line,
    rebuild_balances_for_employees_from_week,
    sync_schedule_from_legacy,
)
from schedules.templatetags.schedule_tags import hours_int

User = get_user_model()


class ScheduleTemplateFilterTests(SimpleTestCase):
    def test_hours_int_preserves_half_hours(self):
        self.assertEqual(hours_int(Decimal("1.50")), "1.5")
        self.assertEqual(hours_int(Decimal("8.00")), "8")

    def test_data_upload_max_number_fields_is_large_enough(self):
        self.assertGreaterEqual(settings.DATA_UPLOAD_MAX_NUMBER_FIELDS, 12000)


class ScheduleCalculationTests(TestCase):
    def setUp(self):
        config = SystemConfiguration.load()
        config.default_weekly_hours = Decimal("46.00")
        config.default_daily_max_hours = Decimal("9.00")
        config.save()

        self.site = Site.objects.create(code="007", name="JARDIN.I", legacy_name="JARDIN IBAGUE")
        self.schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 7, 5),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ShiftTemplate.objects.create(
            code="T1",
            label="06:00-10:00",
            start_time=time(6, 0),
            end_time=time(10, 0),
            duration_hours=Decimal("4.00"),
            display_order=1,
        )
        ShiftTemplate.objects.create(
            code="T2",
            label="13:00-17:00",
            start_time=time(13, 0),
            end_time=time(17, 0),
            duration_hours=Decimal("4.00"),
            display_order=2,
        )
        ShiftTemplate.objects.create(
            code="T3",
            label="08:00-16:00",
            start_time=time(8, 0),
            end_time=time(16, 0),
            duration_hours=Decimal("8.00"),
            display_order=3,
        )
        ShiftTemplate.objects.create(
            code="TN",
            label="18:00-00:00",
            start_time=time(18, 0),
            end_time=time(0, 0),
            duration_hours=Decimal("6.00"),
            display_order=4,
        )

    def build_form_data(self, **overrides):
        data = {}
        for index in range(7):
            data[f"day_{index}_shift_1"] = ""
            data[f"day_{index}_shift_2"] = ""
            data[f"day_{index}_compensation_mode"] = ""
            data[f"day_{index}_compensation_hours"] = ""

        data.update(overrides)
        return data

    def test_parse_shift_hours_works_for_plain_range(self):
        self.assertEqual(parse_shift_hours("06:00-10:00"), Decimal("4.00"))

    def test_recalculate_line_sets_totals_and_variance(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="123",
            employee_name="Empleado Demo",
            weekly_target_hours=Decimal("8.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="06:00-10:00",
            day_0_shift_2="13:00-17:00",
        )

        recalculate_schedule_line(line)

        self.assertEqual(line.day_0_hours, Decimal("8.00"))
        self.assertEqual(line.total_hours, Decimal("8.00"))
        self.assertEqual(line.overtime_hours, Decimal("0.00"))
        self.assertEqual(line.special_days_generated, 1)
        self.assertEqual(line.accrued_total_hours_balance, Decimal("1.33"))

    def test_recalculate_line_accumulates_prior_balance_and_current_payments(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="128",
            employee_name="Empleado Saldo",
            daily_max_hours=Decimal("8.00"),
            accrued_day_balance=Decimal("1.00"),
            accrued_hour_balance=Decimal("5.50"),
            accrued_total_hours_balance=Decimal("13.50"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="128",
            employee_name="Empleado Saldo",
            weekly_target_hours=Decimal("4.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="08:00-16:00",
            day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
        )

        recalculate_schedule_line(line)

        self.assertEqual(line.expected_weekly_hours, Decimal("4.00"))
        self.assertEqual(line.overtime_hours, Decimal("4.00"))
        self.assertEqual(line.payment_days_used, 1)
        self.assertEqual(line.payment_hours_used, Decimal("0.00"))
        self.assertEqual(line.accrued_day_balance, Decimal("1.00"))
        self.assertEqual(line.accrued_hour_balance, Decimal("9.50"))
        self.assertEqual(line.accrued_total_hours_balance, Decimal("10.17"))

    def test_recalculate_line_tracks_money_day_and_money_hour_payments_separately(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="128B",
            employee_name="Empleado Pago Dinero",
            daily_max_hours=Decimal("8.00"),
            accrued_day_balance=Decimal("3.00"),
            accrued_hour_balance=Decimal("6.00"),
            accrued_total_hours_balance=Decimal("30.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="128B",
            employee_name="Empleado Pago Dinero",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_compensation_mode=ScheduleLine.CompensationMode.PAY_MONEY_DAY,
            day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_MONEY_HOURS,
            day_1_compensation_hours=Decimal("2.00"),
            day_1_shift_1="08:00-16:00",
            day_2_shift_1="08:00-16:00",
            day_3_shift_1="08:00-16:00",
            day_4_shift_1="08:00-16:00",
            day_5_shift_1="08:00-16:00",
            day_6_shift_1="18:00-00:00",
        )

        recalculate_schedule_line(line)

        self.assertEqual(line.money_payment_days_used, 1)
        self.assertEqual(line.money_payment_hours_used, Decimal("2.00"))
        self.assertEqual(line.accrued_day_balance, Decimal("2.00"))
        self.assertEqual(line.accrued_hour_balance, Decimal("4.00"))
        self.assertEqual(line.accrued_total_hours_balance, Decimal("19.34"))

    def test_recalculate_line_tracks_night_bonus_hours(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="126",
            employee_name="Empleado Noche",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="18:00-00:00",
        )

        recalculate_schedule_line(line)

        self.assertEqual(line.day_0_hours, Decimal("6.00"))
        self.assertEqual(line.night_bonus_hours, Decimal("5.00"))

    def test_recalculate_line_uses_initial_balance_when_no_prior_week_exists(self):
        EmployeeInitialBalance.objects.create(
            employee_identifier="140",
            employee_name="Empleado Inicial",
            initial_day_balance=Decimal("2.00"),
            initial_hour_balance=Decimal("3.50"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="140",
            employee_name="Empleado Inicial",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
            day_1_shift_1="08:00-16:00",
            day_2_shift_1="08:00-16:00",
            day_3_shift_1="08:00-16:00",
            day_4_shift_1="08:00-16:00",
            day_5_shift_1="08:00-16:00",
            day_6_shift_1="18:00-00:00",
        )

        recalculate_schedule_line(line)

        self.assertEqual(line.accrued_day_balance, Decimal("2.00"))
        self.assertEqual(line.accrued_hour_balance, Decimal("3.50"))
        self.assertEqual(line.accrued_total_hours_balance, Decimal("18.84"))

    def test_form_accepts_pay_day_using_same_week_special_day_generated_before(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="141",
            employee_name="Empleado Semana",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="08:00-16:00",
                day_3_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_form_accepts_pay_hours_using_same_week_overtime_generated_before(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="141A",
            employee_name="Empleado Horas Semana",
            weekly_target_hours=Decimal("42.00"),
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="08:00-16:00",
                day_2_shift_1="18:00-00:00",
                day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                day_2_compensation_hours="1",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_form_rejects_weekly_overtime_over_medical_restriction(self):
        EmployeeOvertimeRestriction.objects.create(
            employee_identifier="141B",
            employee_name="Empleado Restringido",
            max_daily_overtime_hours=Decimal("8.00"),
            max_weekly_overtime_hours=Decimal("0.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="141B",
            employee_name="Empleado Restringido",
            weekly_target_hours=Decimal("8.00"),
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="08:00-16:00",
                day_1_shift_1="08:00-16:00",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertTrue(
            any("restriccion medica" in error.lower() for error in form.non_field_errors()),
            form.errors,
        )

    def test_form_rejects_daily_overtime_over_medical_restriction(self):
        EmployeeOvertimeRestriction.objects.create(
            employee_identifier="141C",
            employee_name="Empleado Restringido Diario",
            max_daily_overtime_hours=Decimal("0.00"),
            max_weekly_overtime_hours=Decimal("20.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="141C",
            employee_name="Empleado Restringido Diario",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="08:00-16:00",
                day_0_shift_2="13:00-17:00",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertTrue(
            "restriccion medica" in form.errors["day_0_shift_2"][0].lower(),
            form.errors,
        )

    def test_copy_schedule_template_brings_previous_week_turns(self):
        source_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=source_schedule,
            employee_identifier="142",
            employee_name="Empleado Base",
            job_role_name="AUXILIAR",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="06:00-10:00",
            day_0_shift_2="13:00-17:00",
            day_1_shift_1="08:00-16:00",
        )
        target_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 6, 14),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        target_line = ScheduleLine.objects.create(
            schedule=target_schedule,
            employee_identifier="142",
            employee_name="Empleado Base",
            job_role_name="AUXILIAR",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
        )

        copied_count, _ = copy_schedule_template(source_schedule, target_schedule)

        target_line.refresh_from_db()
        self.assertEqual(copied_count, 1)
        self.assertEqual(target_line.day_0_shift_1, "06:00-10:00")
        self.assertEqual(target_line.day_0_shift_2, "13:00-17:00")
        self.assertEqual(target_line.day_1_shift_1, "08:00-16:00")

    def test_schedule_load_form_rejects_non_sunday_start_date(self):
        admin_user = User.objects.create_user(username="admin_semana", password="secret")
        access = UserSiteAccess.objects.get(user=admin_user)
        access.role = UserSiteAccess.Role.ADMIN
        access.save()
        form = ScheduleLoadForm(
            data={
                "site": str(self.site.pk),
                "week_start_date": "2026-06-08",
                "copy_from_schedule": "",
            },
            user=admin_user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("domingos", form.errors["week_start_date"][0].lower())

    def test_compact_alert_summary_groups_main_categories(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="133",
            employee_name="Empleado Resumen",
            weekly_target_hours=Decimal("42.00"),
            daily_max_hours=Decimal("8.00"),
        )

        recalculate_schedule_line(line)

        compact_summary = get_schedule_line_compact_alert_summary(line)
        self.assertIn("horas esperadas", compact_summary.lower())
        self.assertNotIn("pendientes", compact_summary.lower())

    def test_schedule_lines_are_ordered_by_role_name_then_employee(self):
        ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="201",
            employee_name="Zulu",
            job_role_name="ZAPATERO",
        )
        ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="202",
            employee_name="Ana",
            job_role_name="AUXILIAR",
        )

        ordered_roles = list(
            self.schedule.lines.values_list("job_role_name", "employee_name")
        )

        self.assertEqual(
            ordered_roles,
            [("AUXILIAR", "Ana"), ("ZAPATERO", "Zulu")],
        )

    def test_form_rejects_second_shift_when_first_is_continuous(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="124",
            employee_name="Empleado Demo 2",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="08:00-16:00",
                day_0_shift_2="13:00-17:00",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("jornadas continuas", form.errors["day_0_shift_2"][0].lower())

    def test_form_rejects_pay_day_without_prior_day_balance(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="130",
            employee_name="Empleado Demo 6",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("hasta ese dia", form.errors["day_2_compensation_mode"][0].lower())

    def test_form_rejects_pay_day_when_only_hour_balance_exists(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="130A",
            employee_name="Empleado Solo Horas",
            accrued_hour_balance=Decimal("8.00"),
            accrued_total_hours_balance=Decimal("8.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="130A",
            employee_name="Empleado Solo Horas",
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("1 dia acumulado", form.errors["day_2_compensation_mode"][0].lower())

    def test_form_rejects_pay_day_on_sunday(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="130AA",
            employee_name="Empleado Domingo",
            accrued_day_balance=Decimal("2.00"),
            accrued_total_hours_balance=Decimal("16.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="130AA",
            employee_name="Empleado Domingo",
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_0_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("domingo o festivo", form.errors["day_0_compensation_mode"][0].lower())

    def test_form_rejects_money_day_when_only_hour_balance_exists(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="130B",
            employee_name="Empleado Dinero Dia",
            accrued_hour_balance=Decimal("8.00"),
            accrued_total_hours_balance=Decimal("8.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="130B",
            employee_name="Empleado Dinero Dia",
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_MONEY_DAY),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
            allow_money_payment=True,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("pago en dinero por dia", form.errors["day_2_compensation_mode"][0].lower())

    def test_form_accepts_money_day_on_holiday(self):
        holiday_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 6, 28),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 6, 21),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="130BB",
            employee_name="Empleado Festivo",
            accrued_day_balance=Decimal("2.00"),
            accrued_total_hours_balance=Decimal("16.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=holiday_schedule,
            employee_identifier="130BB",
            employee_name="Empleado Festivo",
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_MONEY_DAY),
            instance=line,
            schedule=holiday_schedule,
            shift_choices=[],
            secondary_shift_choices=[],
            allow_money_payment=True,
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_form_accepts_money_day_with_prior_day_balance(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="130C",
            employee_name="Empleado Dinero Dia Valido",
            accrued_day_balance=Decimal("2.00"),
            accrued_total_hours_balance=Decimal("16.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="130C",
            employee_name="Empleado Dinero Dia Valido",
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_MONEY_DAY),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
            allow_money_payment=True,
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_form_pay_day_converts_day_to_rest_automatically(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="131",
            employee_name="Empleado Demo 7",
            daily_max_hours=Decimal("8.00"),
            accrued_day_balance=Decimal("1.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="131",
            employee_name="Empleado Demo 7",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_2_shift_1="08:00-16:00",
                day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["day_2_shift_1"], get_rest_shift_label())
        self.assertEqual(form.cleaned_data["day_2_shift_2"], "")

    def test_form_rejects_advance_day_when_limit_is_already_reached(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="131A",
            employee_name="Empleado Tope Adelantado",
            daily_max_hours=Decimal("8.00"),
            accrued_day_balance=Decimal("-2.00"),
            advance_rest_pending_balance=Decimal("2.00"),
            accrued_total_hours_balance=Decimal("-16.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="131A",
            employee_name="Empleado Tope Adelantado",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_2_compensation_mode=ScheduleLine.CompensationMode.ADVANCE_DAY),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("limite maximo de 2 dias adelantados", form.errors["day_2_compensation_mode"][0].lower())

    def test_form_only_allows_one_more_advance_day_when_pending_balance_is_minus_one(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="131B",
            employee_name="Empleado Adelantado Parcial",
            daily_max_hours=Decimal("8.00"),
            accrued_day_balance=Decimal("-1.00"),
            advance_rest_pending_balance=Decimal("1.00"),
            accrued_total_hours_balance=Decimal("-8.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="131B",
            employee_name="Empleado Adelantado Parcial",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_2_compensation_mode=ScheduleLine.CompensationMode.ADVANCE_DAY,
                day_3_compensation_mode=ScheduleLine.CompensationMode.ADVANCE_DAY,
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertNotIn("day_2_compensation_mode", form.errors)
        self.assertIn("limite maximo de 2 dias adelantados", form.errors["day_3_compensation_mode"][0].lower())

    def test_form_rejects_pay_hours_over_prior_available_balance(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="132",
            employee_name="Empleado Demo 8",
            accrued_hour_balance=Decimal("1.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="132",
            employee_name="Empleado Demo 8",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_1_shift_1="18:00-00:00",
                day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                day_1_compensation_hours="1.5",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("saldo acumulado disponible hasta ese dia", form.errors["day_1_compensation_hours"][0].lower())

    def test_form_rejects_pay_hours_above_needed_for_daily_journey(self):
        prior_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 5, 31),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=prior_schedule,
            employee_identifier="134",
            employee_name="Empleado Demo 9",
            daily_max_hours=Decimal("8.00"),
            accrued_hour_balance=Decimal("5.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="134",
            employee_name="Empleado Demo 9",
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_1_shift_1="06:00-10:00",
                day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                day_1_compensation_hours="5",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("completar la jornada", form.errors["day_1_compensation_hours"][0].lower())

    def test_form_rejects_second_shift_when_first_shift_already_meets_daily_journey(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="135",
            employee_name="Empleado Demo 10",
            weekly_target_hours=Decimal("24.00"),
            daily_max_hours=Decimal("4.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_1_shift_1="06:00-10:00",
                day_1_shift_2="13:00-17:00",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("cumple la jornada", form.errors["day_1_shift_2"][0].lower())

    def test_same_week_balance_is_carried_by_employee_between_sites(self):
        first_site = Site.objects.create(code="009", name="RIOJA")
        second_site = Site.objects.create(code="001", name="ALAMOS")
        first_schedule = WeeklySchedule.objects.create(
            site=first_site,
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        second_schedule = WeeklySchedule.objects.create(
            site=second_site,
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        EmployeeInitialBalance.objects.create(
            employee_identifier="2000",
            employee_name="Empleado Trasladado",
            initial_day_balance=Decimal("6.00"),
        )
        first_line = ScheduleLine.objects.create(
            schedule=first_schedule,
            employee_identifier="2000",
            employee_name="Empleado Trasladado",
            weekly_target_hours=Decimal("36.00"),
            daily_max_hours=Decimal("8.00"),
            day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            day_3_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            day_4_shift_1="18:00-00:00",
            day_5_shift_1="18:00-00:00",
            day_6_shift_1="18:00-00:00",
        )
        second_line = ScheduleLine.objects.create(
            schedule=second_schedule,
            employee_identifier="2000",
            employee_name="Empleado Trasladado",
            weekly_target_hours=Decimal("36.00"),
            daily_max_hours=Decimal("8.00"),
        )

        rebuild_balances_for_employees_from_week(first_schedule.week_start_date, ["2000"])
        first_line.refresh_from_db()
        second_line.refresh_from_db()

        self.assertEqual(first_line.accrued_day_balance, Decimal("3.00"))
        self.assertEqual(second_line.accrued_day_balance, Decimal("3.00"))
        self.assertEqual(second_line.accrued_hour_balance, Decimal("0.00"))

    def test_same_week_balance_uses_real_day_order_even_if_later_site_schedule_was_created_first(self):
        later_site_schedule = WeeklySchedule.objects.create(
            site=Site.objects.create(code="014", name="JARDIN.N"),
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        earlier_site_schedule = WeeklySchedule.objects.create(
            site=Site.objects.create(code="015", name="RIOJA"),
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        EmployeeInitialBalance.objects.create(
            employee_identifier="2001",
            employee_name="Empleado Orden Real",
            initial_day_balance=Decimal("6.00"),
        )
        later_line = ScheduleLine.objects.create(
            schedule=later_site_schedule,
            employee_identifier="2001",
            employee_name="Empleado Orden Real",
            daily_max_hours=Decimal("8.00"),
            day_4_shift_1="08:00-16:00",
            day_5_shift_1="08:00-16:00",
            day_6_shift_1="08:00-16:00",
        )
        earlier_line = ScheduleLine.objects.create(
            schedule=earlier_site_schedule,
            employee_identifier="2001",
            employee_name="Empleado Orden Real",
            daily_max_hours=Decimal("8.00"),
            day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            day_3_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
        )

        rebuild_balances_for_employees_from_week(earlier_site_schedule.week_start_date, ["2001"])
        earlier_line.refresh_from_db()
        later_line.refresh_from_db()

        self.assertEqual(earlier_line.accrued_day_balance, Decimal("3.00"))
        self.assertEqual(later_line.accrued_day_balance, Decimal("3.00"))

    def test_same_week_manual_adjustment_is_carried_to_following_site_balance(self):
        later_site_schedule = WeeklySchedule.objects.create(
            site=Site.objects.create(code="016", name="JARDIN.N"),
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        earlier_site_schedule = WeeklySchedule.objects.create(
            site=Site.objects.create(code="017", name="RIOJA"),
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        EmployeeInitialBalance.objects.create(
            employee_identifier="2002",
            employee_name="Empleado Ajuste",
            initial_day_balance=Decimal("6.00"),
        )
        later_line = ScheduleLine.objects.create(
            schedule=later_site_schedule,
            employee_identifier="2002",
            employee_name="Empleado Ajuste",
            daily_max_hours=Decimal("8.00"),
            day_4_shift_1="08:00-16:00",
            day_5_shift_1="08:00-16:00",
        )
        earlier_line = ScheduleLine.objects.create(
            schedule=earlier_site_schedule,
            employee_identifier="2002",
            employee_name="Empleado Ajuste",
            daily_max_hours=Decimal("8.00"),
            day_1_shift_1="08:00-16:00",
            day_2_shift_1="08:00-16:00",
            day_3_shift_1="08:00-16:00",
            manual_day_adjustment=Decimal("-3.00"),
        )

        rebuild_balances_for_employees_from_week(earlier_site_schedule.week_start_date, ["2002"])
        earlier_line.refresh_from_db()
        later_line.refresh_from_db()

        self.assertEqual(earlier_line.accrued_day_balance, Decimal("4.00"))
        self.assertEqual(later_line.accrued_day_balance, Decimal("4.00"))

    @patch("schedules.services.fetch_active_staff_for_site", return_value=[])
    def test_sync_schedule_uses_schedule_week_start_for_legacy_period(self, mock_fetch_staff):
        created_count, updated_count = sync_schedule_from_legacy(self.schedule)

        self.assertEqual(created_count, 0)
        self.assertEqual(updated_count, 0)
        mock_fetch_staff.assert_called_once_with(
            self.site.code,
            week_start_date=self.schedule.week_start_date,
        )

    @patch("schedules.services.fetch_active_staff_for_site")
    def test_sync_schedule_excludes_blacklisted_employee(self, mock_fetch_staff):
        EmployeeScheduleBlacklist.objects.create(
            employee_identifier="36178712",
            employee_name="Persona Bloqueada",
        )
        mock_fetch_staff.return_value = [
            OperationalStaffingRecord(
                employee_id="36178712",
                employee_name="Persona Bloqueada",
                site_code=self.site.code,
                department_code="A1",
                department_name="ABASTOS",
                role_code="AUX",
                role_name="AUXILIAR",
            ),
            OperationalStaffingRecord(
                employee_id="99887766",
                employee_name="Persona Permitida",
                site_code=self.site.code,
                department_code="B1",
                department_name="CARNES",
                role_code="CARN",
                role_name="CARNICERO",
            ),
        ]

        created_count, updated_count = sync_schedule_from_legacy(self.schedule)

        self.assertEqual(created_count, 1)
        self.assertEqual(updated_count, 0)
        self.assertFalse(
            ScheduleLine.objects.filter(schedule=self.schedule, employee_identifier="36178712").exists()
        )
        self.assertTrue(
            ScheduleLine.objects.filter(schedule=self.schedule, employee_identifier="99887766").exists()
        )

    def test_sync_schedule_loads_blacklisted_staff_into_personal_vario(self):
        personal_vario, _ = Site.objects.get_or_create(
            code=Site.PERSONAL_VARIO_CODE,
            defaults={
                "name": Site.PERSONAL_VARIO_NAME,
                "admin_only": True,
                "is_active": True,
            },
        )
        OperationalStaffCache.objects.create(
            site_code="007",
            employee_identifier="36178712",
            employee_name="Persona Bloqueada",
            department_code="TR",
            department_name="TRANSPORTE",
            role_code="COND",
            role_name="CONDUCTOR",
        )
        EmployeeScheduleBlacklist.objects.create(
            employee_identifier="36178712",
            employee_name="Persona Bloqueada",
        )
        schedule = WeeklySchedule.objects.create(
            site=personal_vario,
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )

        created_count, updated_count = sync_schedule_from_legacy(schedule)

        self.assertEqual(created_count, 1)
        self.assertEqual(updated_count, 0)
        created_line = ScheduleLine.objects.get(schedule=schedule, employee_identifier="36178712")
        self.assertEqual(created_line.employee_name, "Persona Bloqueada")
        self.assertEqual(created_line.job_role_name, "CONDUCTOR")
        self.assertEqual(created_line.department_name, "TRANSPORTE")


class ProportionalWeeklyBalanceTests(TestCase):
    def setUp(self):
        config = SystemConfiguration.load()
        config.default_weekly_hours = Decimal("42.00")
        config.default_daily_max_hours = Decimal("9.00")
        config.save()
        Holiday.objects.all().delete()
        base_holidays_patch = patch("schedules.calendar_utils.get_base_colombian_holidays", return_value=set())
        self.addCleanup(base_holidays_patch.stop)
        base_holidays_patch.start()
        self.site = Site.objects.create(code="042", name="PRUEBA")

    def ensure_holiday(self, holiday_date: date, name: str = "Festivo prueba"):
        Holiday.objects.get_or_create(holiday_date=holiday_date, defaults={"name": name})

    def build_line(
        self,
        *,
        week_start: date,
        employee_identifier: str,
        weekly_target_hours: Decimal,
        shift_map: dict[int, str] | None = None,
        second_shift_map: dict[int, str] | None = None,
        compensation_map: dict[int, str] | None = None,
        daily_max_hours: Decimal = Decimal("9.00"),
    ) -> ScheduleLine:
        schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=week_start,
            first_day_index=SystemConfiguration.SUNDAY,
        )
        payload = {
            "schedule": schedule,
            "employee_identifier": employee_identifier,
            "employee_name": f"Empleado {employee_identifier}",
            "job_role_name": "AUXILIAR",
            "weekly_target_hours": weekly_target_hours,
            "daily_max_hours": daily_max_hours,
        }
        for index, label in (shift_map or {}).items():
            payload[f"day_{index}_shift_1"] = label
        for index, label in (second_shift_map or {}).items():
            payload[f"day_{index}_shift_2"] = label
        for index, mode in (compensation_map or {}).items():
            payload[f"day_{index}_compensation_mode"] = mode
        return ScheduleLine.objects.create(**payload)

    def rebuild_employee(self, week_start: date, employee_identifier: str):
        rebuild_balances_for_employees_from_week(week_start, [employee_identifier])

    def test_42_hour_week_without_holidays_balances_to_zero(self):
        line = self.build_line(
            week_start=date(2026, 8, 2),
            employee_identifier="4201",
            weekly_target_hours=Decimal("42.00"),
            shift_map={1: "09:00-16:00", 2: "09:00-16:00", 3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_work_days, 6)
        self.assertEqual(line.expected_weekly_hours, Decimal("42.00"))
        self.assertEqual(line.total_hours, Decimal("42.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("0.00"))
        self.assertEqual(line.accrued_hour_balance, Decimal("0.00"))

    def test_42_hour_week_with_one_holiday_balances_to_zero(self):
        self.ensure_holiday(date(2026, 8, 11), "Festivo martes")
        line = self.build_line(
            week_start=date(2026, 8, 9),
            employee_identifier="4202",
            weekly_target_hours=Decimal("42.00"),
            shift_map={1: "09:00-16:00", 3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_work_days, 5)
        self.assertEqual(line.expected_weekly_hours, Decimal("35.00"))
        self.assertEqual(line.total_hours, Decimal("35.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("0.00"))
        self.assertEqual(line.accrued_hour_balance, Decimal("0.00"))

    def test_compensatory_days_reduce_expected_hours_proportionally(self):
        EmployeeInitialBalance.objects.create(
            employee_identifier="4203",
            employee_name="Empleado 4203",
            initial_day_balance=Decimal("2.00"),
        )
        line = self.build_line(
            week_start=date(2026, 8, 16),
            employee_identifier="4203",
            weekly_target_hours=Decimal("42.00"),
            shift_map={3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
            compensation_map={1: ScheduleLine.CompensationMode.PAY_DAY, 2: ScheduleLine.CompensationMode.PAY_DAY},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_work_days, 4)
        self.assertEqual(line.expected_weekly_hours, Decimal("28.00"))
        self.assertEqual(line.total_hours, Decimal("28.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("0.00"))
        self.assertEqual(line.accrued_day_balance, Decimal("0.00"))

    def test_underworked_week_generates_negative_hour_balance(self):
        EmployeeInitialBalance.objects.create(
            employee_identifier="4204",
            employee_name="Empleado 4204",
            initial_day_balance=Decimal("2.00"),
        )
        line = self.build_line(
            week_start=date(2026, 8, 23),
            employee_identifier="4204",
            weekly_target_hours=Decimal("42.00"),
            shift_map={3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-12:00"},
            compensation_map={1: ScheduleLine.CompensationMode.PAY_DAY, 2: ScheduleLine.CompensationMode.PAY_DAY},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_weekly_hours, Decimal("28.00"))
        self.assertEqual(line.total_hours, Decimal("24.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("-4.00"))
        self.assertEqual(line.accrued_hour_balance, Decimal("-4.00"))
        self.assertIn("No cumple las horas esperadas de la semana", line.validation_summary)

    def test_future_extra_hours_offset_prior_negative_hour_balance_first(self):
        first_line = self.build_line(
            week_start=date(2026, 8, 30),
            employee_identifier="4205",
            weekly_target_hours=Decimal("42.00"),
            shift_map={1: "09:00-16:00", 2: "09:00-16:00", 3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-12:00"},
        )
        second_line = self.build_line(
            week_start=date(2026, 9, 6),
            employee_identifier="4205",
            weekly_target_hours=Decimal("42.00"),
            shift_map={1: "08:00-16:00", 2: "08:00-16:00", 3: "08:00-16:00", 4: "08:00-16:00", 5: "08:00-16:00", 6: "08:00-16:00"},
        )

        self.rebuild_employee(first_line.schedule.week_start_date, first_line.employee_identifier)
        first_line.refresh_from_db()
        second_line.refresh_from_db()

        self.assertEqual(first_line.accrued_hour_balance, Decimal("-4.00"))
        self.assertEqual(second_line.weekly_hour_difference, Decimal("6.00"))
        self.assertEqual(second_line.accrued_hour_balance, Decimal("2.00"))

    def test_44_hour_week_uses_proportional_daily_base(self):
        line = self.build_line(
            week_start=date(2026, 9, 13),
            employee_identifier="4401",
            weekly_target_hours=Decimal("44.00"),
            shift_map={1: "08:00-16:00", 2: "08:00-16:00", 3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()
        snapshot = get_schedule_line_balance_snapshot(line)

        self.assertEqual(snapshot["day_reference_hours"], Decimal("7.33"))
        self.assertEqual(line.expected_weekly_hours, Decimal("44.00"))
        self.assertEqual(line.total_hours, Decimal("44.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("0.00"))

    def test_52_hour_week_uses_proportional_daily_base(self):
        line = self.build_line(
            week_start=date(2026, 9, 20),
            employee_identifier="5201",
            weekly_target_hours=Decimal("52.00"),
            shift_map={1: "08:00-17:00", 2: "08:00-17:00", 3: "08:00-17:00", 4: "08:00-17:00", 5: "08:00-16:00", 6: "08:00-16:00"},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()
        snapshot = get_schedule_line_balance_snapshot(line)

        self.assertEqual(snapshot["day_reference_hours"], Decimal("8.67"))
        self.assertEqual(line.expected_weekly_hours, Decimal("52.00"))
        self.assertEqual(line.total_hours, Decimal("52.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("0.00"))

    def test_advance_rest_days_create_negative_day_balance(self):
        line = self.build_line(
            week_start=date(2026, 9, 27),
            employee_identifier="4208",
            weekly_target_hours=Decimal("42.00"),
            shift_map={3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
            compensation_map={1: ScheduleLine.CompensationMode.ADVANCE_DAY, 2: ScheduleLine.CompensationMode.ADVANCE_DAY},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_weekly_hours, Decimal("28.00"))
        self.assertEqual(line.accrued_day_balance, Decimal("-2.00"))
        self.assertEqual(line.advance_rest_pending_balance, Decimal("2.00"))

    def test_advance_rest_days_never_exceed_two_pending_days(self):
        line = self.build_line(
            week_start=date(2026, 10, 4),
            employee_identifier="4208A",
            weekly_target_hours=Decimal("42.00"),
            shift_map={4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
            compensation_map={
                1: ScheduleLine.CompensationMode.ADVANCE_DAY,
                2: ScheduleLine.CompensationMode.ADVANCE_DAY,
                3: ScheduleLine.CompensationMode.ADVANCE_DAY,
            },
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_weekly_hours, Decimal("21.00"))
        self.assertEqual(line.accrued_day_balance, Decimal("-2.00"))
        self.assertEqual(line.advance_rest_pending_balance, Decimal("2.00"))
        self.assertIn("limite maximo de 2 dias adelantados", line.validation_summary.lower())

    def test_generated_day_offsets_pending_advance_rest_first(self):
        first_line = self.build_line(
            week_start=date(2026, 10, 4),
            employee_identifier="4209",
            weekly_target_hours=Decimal("42.00"),
            shift_map={3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
            compensation_map={1: ScheduleLine.CompensationMode.ADVANCE_DAY, 2: ScheduleLine.CompensationMode.ADVANCE_DAY},
        )
        second_line = self.build_line(
            week_start=date(2026, 10, 11),
            employee_identifier="4209",
            weekly_target_hours=Decimal("42.00"),
            shift_map={0: "09:00-16:00", 1: "descanso", 2: "09:00-16:00", 3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
        )

        self.rebuild_employee(first_line.schedule.week_start_date, first_line.employee_identifier)
        first_line.refresh_from_db()
        second_line.refresh_from_db()

        self.assertEqual(first_line.advance_rest_pending_balance, Decimal("2.00"))
        self.assertEqual(second_line.accrued_day_balance, Decimal("-1.00"))
        self.assertEqual(second_line.advance_rest_pending_balance, Decimal("1.00"))

    def test_shifted_weekly_rest_with_sunday_work_does_not_reduce_expected_hours_twice(self):
        line = self.build_line(
            week_start=date(2026, 10, 18),
            employee_identifier="4209A",
            weekly_target_hours=Decimal("50.00"),
            shift_map={
                0: "07:00-12:00",
                1: "07:00-12:00",
                2: "07:00-12:00",
                4: "07:00-12:00",
                5: "07:00-12:00",
                6: "07:00-12:00",
            },
            second_shift_map={
                0: "13:00-17:00",
                1: "13:00-17:00",
                2: "13:00-17:00",
                4: "13:00-17:00",
                5: "13:00-17:00",
                6: "13:00-17:00",
            },
            compensation_map={3: ScheduleLine.CompensationMode.PAY_DAY},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_work_days, 6)
        self.assertEqual(line.expected_weekly_hours, Decimal("50.00"))
        self.assertEqual(line.total_hours, Decimal("54.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("4.00"))

    def test_holiday_and_shifted_weekly_rest_only_reduce_expected_hours_once_each(self):
        self.ensure_holiday(date(2026, 10, 20), "Festivo martes")
        line = self.build_line(
            week_start=date(2026, 10, 18),
            employee_identifier="4209B",
            weekly_target_hours=Decimal("50.00"),
            shift_map={
                0: "07:00-12:00",
                1: "07:00-12:00",
                2: "07:00-12:00",
                4: "07:00-12:00",
                5: "07:00-12:00",
            },
            second_shift_map={
                0: "13:00-17:00",
                1: "13:00-17:00",
                2: "13:00-17:00",
                4: "13:00-17:00",
                5: "13:00-17:00",
            },
            compensation_map={3: ScheduleLine.CompensationMode.PAY_DAY},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_work_days, 5)
        self.assertEqual(line.expected_weekly_hours, Decimal("41.67"))
        self.assertEqual(line.total_hours, Decimal("45.00"))
        self.assertEqual(line.weekly_hour_difference, Decimal("3.33"))

    def test_two_generated_days_can_leave_positive_balance_after_crossing_negative_pending(self):
        self.ensure_holiday(date(2026, 10, 21), "Festivo miercoles")
        first_line = self.build_line(
            week_start=date(2026, 10, 11),
            employee_identifier="4210",
            weekly_target_hours=Decimal("42.00"),
            shift_map={3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
            compensation_map={1: ScheduleLine.CompensationMode.ADVANCE_DAY},
        )
        second_line = self.build_line(
            week_start=date(2026, 10, 18),
            employee_identifier="4210",
            weekly_target_hours=Decimal("42.00"),
            shift_map={0: "09:00-16:00", 1: "descanso", 2: "09:00-16:00", 3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
        )

        self.rebuild_employee(first_line.schedule.week_start_date, first_line.employee_identifier)
        first_line.refresh_from_db()
        second_line.refresh_from_db()

        self.assertEqual(first_line.accrued_day_balance, Decimal("-1.00"))
        self.assertEqual(second_line.accrued_day_balance, Decimal("1.00"))
        self.assertEqual(second_line.advance_rest_pending_balance, Decimal("0.00"))

    def test_balances_do_not_cross_between_different_workers(self):
        EmployeeInitialBalance.objects.create(employee_identifier="4211A", employee_name="Empleado A", initial_day_balance=Decimal("2.00"))
        EmployeeInitialBalance.objects.create(employee_identifier="4211B", employee_name="Empleado B", initial_hour_balance=Decimal("3.00"))
        schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 10, 25),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        first_line = ScheduleLine.objects.create(
            schedule=schedule,
            employee_identifier="4211A",
            employee_name="Empleado 4211A",
            job_role_name="AUXILIAR",
            weekly_target_hours=Decimal("42.00"),
            daily_max_hours=Decimal("9.00"),
            day_1_shift_1="09:00-16:00",
            day_2_shift_1="09:00-16:00",
            day_3_shift_1="09:00-16:00",
            day_4_shift_1="09:00-16:00",
            day_5_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
        )
        second_line = ScheduleLine.objects.create(
            schedule=schedule,
            employee_identifier="4211B",
            employee_name="Empleado 4211B",
            job_role_name="AUXILIAR",
            weekly_target_hours=Decimal("42.00"),
            daily_max_hours=Decimal("9.00"),
            day_1_shift_1="09:00-16:00",
            day_2_shift_1="09:00-16:00",
            day_3_shift_1="09:00-16:00",
            day_4_shift_1="09:00-16:00",
            day_5_shift_1="09:00-12:00",
        )

        self.rebuild_employee(first_line.schedule.week_start_date, "4211A")
        self.rebuild_employee(second_line.schedule.week_start_date, "4211B")
        first_line.refresh_from_db()
        second_line.refresh_from_db()

        self.assertEqual(first_line.accrued_day_balance, Decimal("1.00"))
        self.assertEqual(second_line.accrued_day_balance, Decimal("0.00"))
        self.assertEqual(second_line.accrued_hour_balance, Decimal("-8.00"))

    def test_holiday_and_advance_rest_do_not_double_discount_expected_days(self):
        self.ensure_holiday(date(2026, 11, 3), "Festivo martes")
        line = self.build_line(
            week_start=date(2026, 11, 1),
            employee_identifier="4212",
            weekly_target_hours=Decimal("42.00"),
            shift_map={3: "09:00-16:00", 4: "09:00-16:00", 5: "09:00-16:00", 6: "09:00-16:00"},
            compensation_map={1: ScheduleLine.CompensationMode.ADVANCE_DAY},
        )

        self.rebuild_employee(line.schedule.week_start_date, line.employee_identifier)
        line.refresh_from_db()

        self.assertEqual(line.expected_work_days, 4)
        self.assertEqual(line.expected_weekly_hours, Decimal("28.00"))
        self.assertEqual(line.total_hours, Decimal("28.00"))
        self.assertEqual(line.accrued_day_balance, Decimal("-1.00"))
        self.assertEqual(line.advance_rest_pending_balance, Decimal("1.00"))


class ScheduleDeleteViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.site = Site.objects.create(code="007", name="JARDIN.I")
        self.personal_vario, _ = Site.objects.get_or_create(
            code=Site.PERSONAL_VARIO_CODE,
            defaults={
                "name": Site.PERSONAL_VARIO_NAME,
                "admin_only": True,
                "is_active": True,
            },
        )
        self.job_role = JobRole.objects.create(
            code="CARN",
            name="Carnicero",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("9.00"),
        )
        self.schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 6, 7),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        self.line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="123",
            employee_name="Empleado Demo",
            weekly_target_hours=Decimal("4.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="08:00-16:00",
        )
        recalculate_schedule_line(self.line)
        self.line.save()
        self.user = User.objects.create_user(username="operador_delete", password="secret")
        access = UserSiteAccess.objects.get(user=self.user)
        access.sites.add(self.site)
        self.admin_user = User.objects.create_user(username="admin_delete", password="secret")
        admin_access = UserSiteAccess.objects.get(user=self.admin_user)
        admin_access.role = UserSiteAccess.Role.ADMIN
        admin_access.save()

    def build_schedule_form_payload(self, *, status=None, notes="", line=None, extra=None):
        active_line = line or self.line
        payload = {
            "status": status or self.schedule.status or WeeklySchedule.Status.DRAFT,
            "notes": notes,
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-id": str(active_line.pk),
        }
        for index in range(7):
            payload[f"lines-0-day_{index}_shift_1"] = getattr(active_line, f"day_{index}_shift_1", "") or ""
            payload[f"lines-0-day_{index}_shift_2"] = getattr(active_line, f"day_{index}_shift_2", "") or ""
            payload[f"lines-0-day_{index}_compensation_mode"] = getattr(
                active_line,
                f"day_{index}_compensation_mode",
                "",
            ) or ""
            compensation_hours = getattr(active_line, f"day_{index}_compensation_hours", "") or ""
            payload[f"lines-0-day_{index}_compensation_hours"] = str(compensation_hours) if compensation_hours else ""
        if extra:
            payload.update(extra)
        return payload

    def test_site_user_cannot_delete_schedule(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:delete", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(WeeklySchedule.objects.filter(pk=self.schedule.pk).exists())
        self.assertEqual(ScheduleLine.objects.count(), 1)

    def test_site_user_does_not_see_delete_action(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:list"),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "plain-action-button")
        self.assertNotContains(response, "Eliminar horario")

    def test_admin_can_delete_schedule(self):
        self.client.login(username="admin_delete", password="secret")

        response = self.client.post(
            reverse("schedules:delete", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("schedules:list"))
        self.assertFalse(WeeklySchedule.objects.filter(pk=self.schedule.pk).exists())
        self.assertEqual(ScheduleLine.objects.count(), 0)

    def test_admin_delete_rebuilds_future_day_balance_after_rest_payment_removed(self):
        EmployeeInitialBalance.objects.create(
            employee_identifier="7001",
            employee_name="Empleado Saldo",
            initial_day_balance=Decimal("4.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="7001",
            employee_name="Empleado Saldo",
            daily_max_hours=Decimal("8.00"),
            day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
        )
        future_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 6, 14),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        future_line = ScheduleLine.objects.create(
            schedule=future_schedule,
            employee_identifier="7001",
            employee_name="Empleado Saldo",
            daily_max_hours=Decimal("8.00"),
        )
        rebuild_balances_for_employees_from_week(self.schedule.week_start_date, ["7001"])
        future_line.refresh_from_db()
        self.assertEqual(future_line.accrued_day_balance, Decimal("2.00"))

        self.client.login(username="admin_delete", password="secret")
        response = self.client.post(
            reverse("schedules:delete", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        future_line.refresh_from_db()
        self.assertEqual(future_line.accrued_day_balance, Decimal("4.00"))
        self.assertFalse(ScheduleLine.objects.filter(pk=line.pk).exists())

    def test_admin_delete_rebuilds_future_day_balance_after_special_day_removed(self):
        EmployeeInitialBalance.objects.create(
            employee_identifier="7002",
            employee_name="Empleado Domingo",
            initial_day_balance=Decimal("4.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="7002",
            employee_name="Empleado Domingo",
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="08:00-16:00",
        )
        future_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 6, 14),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        future_line = ScheduleLine.objects.create(
            schedule=future_schedule,
            employee_identifier="7002",
            employee_name="Empleado Domingo",
            daily_max_hours=Decimal("8.00"),
        )
        rebuild_balances_for_employees_from_week(self.schedule.week_start_date, ["7002"])
        future_line.refresh_from_db()
        self.assertEqual(future_line.accrued_day_balance, Decimal("5.00"))

        self.client.login(username="admin_delete", password="secret")
        response = self.client.post(
            reverse("schedules:delete", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        future_line.refresh_from_db()
        self.assertEqual(future_line.accrued_day_balance, Decimal("4.00"))
        self.assertFalse(ScheduleLine.objects.filter(pk=line.pk).exists())

    def test_admin_sees_delete_action(self):
        self.client.login(username="admin_delete", password="secret")

        response = self.client.get(
            reverse("schedules:list"),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "plain-action-button")
        self.assertContains(response, "Excel")
        self.assertContains(response, "Cargar saldos iniciales")

    @patch("schedules.forms.lookup_third_party_by_identifier", return_value=None)
    def test_site_user_can_add_manual_schedule_line(self, _mock_lookup):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "manual_add_submit": "1",
                "manual-lookup_attempts": "2",
                "manual-employee_identifier": "987654321",
                "manual-employee_name": "Trabajador Manual",
                "manual-job_role": str(self.job_role.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("schedules:edit", kwargs={"pk": self.schedule.pk}))
        manual_line = ScheduleLine.objects.get(schedule=self.schedule, employee_identifier="987654321")
        self.assertEqual(manual_line.employee_document_type, "")
        self.assertEqual(manual_line.employee_name, "Trabajador Manual")
        self.assertEqual(manual_line.job_role_name, self.job_role.name)
        self.assertEqual(manual_line.department_name, "")

    def test_schedule_edit_collapses_manual_add_and_hides_area_field(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "manual-add-disclosure", html=False)
        self.assertContains(response, "Agregar persona manualmente")
        self.assertNotContains(response, 'name="manual-department"', html=False)
        self.assertNotContains(response, 'name="manual-employee_document_type"', html=False)
        self.assertContains(response, "manual-add-submit-button", html=False)

    def test_schedule_edit_hides_area_column_and_shows_remove_action(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<th>Area</th>", html=False)
        self.assertContains(response, "Retirar")

    def test_schedule_edit_shows_role_filter(self):
        ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="456",
            employee_name="Empleado Cargo",
            job_role_name="Carnicero",
        )
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Filtrar por cargo")
        self.assertContains(response, '<option value="Carnicero">Carnicero</option>', html=False)

    def test_schedule_edit_shows_inventory_checkbox(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-inventory-checkbox="true"', html=False)
        self.assertContains(response, "Inventario")

    def test_schedule_edit_persists_inventory_selection_in_database(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            self.build_schedule_form_payload(
                status=WeeklySchedule.Status.DRAFT,
                notes="Inventario semanal",
                extra={
                    "lines-0-day_1_inventory": "on",
                    "lines-0-manual_day_adjustment": "",
                    "lines-0-manual_hour_adjustment": "",
                },
            ),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        self.line.refresh_from_db()
        self.assertTrue(self.line.day_1_inventory)
        self.assertFalse(self.line.day_0_inventory)
        self.assertEqual(self.line.inventory_days_total(), 1)
        self.assertIn("08/06/2026", self.line.inventory_days_summary())

    def test_schedule_edit_autosaves_without_redirect(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            self.build_schedule_form_payload(
                status=WeeklySchedule.Status.DRAFT,
                notes="Autoguardado silencioso",
                extra={"lines-0-day_1_inventory": "on"},
            ),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_X_SCHEDULE_AUTOSAVE="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.line.refresh_from_db()
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.notes, "Autoguardado silencioso")
        self.assertTrue(self.line.day_1_inventory)

    def test_site_user_can_remove_schedule_line(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "remove_line_id": str(self.line.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("schedules:edit", kwargs={"pk": self.schedule.pk}))
        self.assertFalse(ScheduleLine.objects.filter(pk=self.line.pk).exists())

    def test_manual_schedule_line_rejects_duplicate_document(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "manual_add_submit": "1",
                "manual-employee_identifier": self.line.employee_identifier,
                "manual-employee_name": "Otro Nombre",
                "manual-job_role": str(self.job_role.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ya existe en este horario")
        self.assertEqual(ScheduleLine.objects.filter(schedule=self.schedule).count(), 1)

    @patch("schedules.forms.lookup_third_party_by_identifier", return_value=None)
    def test_manual_schedule_line_rejects_blacklisted_document(self, _mock_lookup):
        EmployeeScheduleBlacklist.objects.create(
            employee_identifier="36178712",
            employee_name="Persona Bloqueada",
        )
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "manual_add_submit": "1",
                "manual-lookup_attempts": "2",
                "manual-employee_identifier": "36178712",
                "manual-employee_name": "Persona Bloqueada",
                "manual-job_role": str(self.job_role.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "lista negra")
        self.assertFalse(
            ScheduleLine.objects.filter(schedule=self.schedule, employee_identifier="36178712").exists()
        )

    def test_site_user_does_not_see_night_hours_column(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Rec. noct.")
        self.assertContains(response, 'data-show-night-hours="false"', html=False)

    def test_admin_sees_night_hours_column(self):
        self.client.login(username="admin_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rec. noct.")
        self.assertContains(response, 'data-show-night-hours="true"', html=False)
    def test_site_user_sees_compact_alert_summary(self):
        self.line.day_0_shift_1 = ""
        self.line.weekly_target_hours = Decimal("42.00")
        recalculate_schedule_line(self.line)
        self.line.save()
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revisa horas esperadas, saldo a favor empresa.")
        self.assertNotContains(response, "No cumple las horas esperadas de la semana")

    def test_admin_sees_detailed_alert_summary(self):
        self.line.day_0_shift_1 = ""
        self.line.weekly_target_hours = Decimal("42.00")
        recalculate_schedule_line(self.line)
        self.line.save()
        self.client.login(username="admin_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No cumple las horas esperadas de la semana")

    def test_published_schedule_hides_save_button_and_shows_closed_notice(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.save()
        self.client.login(username="admin_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "quedo cerrado para edicion")
        self.assertNotContains(response, "Guardar cambios")
        self.assertContains(response, "Habilitar edicion")

    def test_published_schedule_shows_view_action_in_list(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.save()
        self.client.login(username="admin_delete", password="secret")

        response = self.client.get(
            reverse("schedules:list"),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ver")
        self.assertNotContains(response, "Editar")

    def test_published_schedule_rejects_post_updates(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.save()
        self.client.login(username="admin_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "status": WeeklySchedule.Status.PUBLISHED,
                "notes": "",
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "1",
                "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-id": str(self.line.pk),
                "lines-0-day_0_shift_1": self.line.day_0_shift_1,
                "lines-0-day_0_shift_2": self.line.day_0_shift_2,
                "lines-0-day_0_compensation_mode": "",
                "lines-0-day_0_compensation_hours": "",
                "lines-0-day_1_shift_1": "",
                "lines-0-day_1_shift_2": "",
                "lines-0-day_1_compensation_mode": "",
                "lines-0-day_1_compensation_hours": "",
                "lines-0-day_2_shift_1": "",
                "lines-0-day_2_shift_2": "",
                "lines-0-day_2_compensation_mode": "",
                "lines-0-day_2_compensation_hours": "",
                "lines-0-day_3_shift_1": "",
                "lines-0-day_3_shift_2": "",
                "lines-0-day_3_compensation_mode": "",
                "lines-0-day_3_compensation_hours": "",
                "lines-0-day_4_shift_1": "",
                "lines-0-day_4_shift_2": "",
                "lines-0-day_4_compensation_mode": "",
                "lines-0-day_4_compensation_hours": "",
                "lines-0-day_5_shift_1": "",
                "lines-0-day_5_shift_2": "",
                "lines-0-day_5_compensation_mode": "",
                "lines-0-day_5_compensation_hours": "",
                "lines-0-day_6_shift_1": "",
                "lines-0-day_6_shift_2": "",
                "lines-0-day_6_compensation_mode": "",
                "lines-0-day_6_compensation_hours": "",
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("schedules:edit", kwargs={"pk": self.schedule.pk}))

    def test_published_schedule_rejects_manual_add(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.save()
        self.client.login(username="admin_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "manual_add_submit": "1",
                "manual-employee_identifier": "111222333",
                "manual-employee_name": "Manual Cerrado",
                "manual-job_role": str(self.job_role.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("schedules:edit", kwargs={"pk": self.schedule.pk}))
        self.assertFalse(
            ScheduleLine.objects.filter(schedule=self.schedule, employee_identifier="111222333").exists()
        )

    def test_published_schedule_rejects_remove_line(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.save()
        self.client.login(username="admin_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "remove_line_id": str(self.line.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ScheduleLine.objects.filter(pk=self.line.pk).exists())

    def test_admin_can_unlock_published_schedule_and_save_it_closed_again(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.save()
        self.client.login(username="admin_delete", password="secret")

        unlock_response = self.client.post(
            reverse("schedules:unlock", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(unlock_response.status_code, 302)
        self.schedule.refresh_from_db()
        self.assertTrue(self.schedule.admin_edit_enabled)
        self.assertFalse(self.schedule.is_closed)

        save_response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "status": WeeklySchedule.Status.PUBLISHED,
                "notes": "Revisado por admin",
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "1",
                "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-id": str(self.line.pk),
                "lines-0-day_0_shift_1": self.line.day_0_shift_1,
                "lines-0-day_0_shift_2": self.line.day_0_shift_2,
                "lines-0-day_0_compensation_mode": "",
                "lines-0-day_0_compensation_hours": "",
                "lines-0-day_1_shift_1": "",
                "lines-0-day_1_shift_2": "",
                "lines-0-day_1_compensation_mode": "",
                "lines-0-day_1_compensation_hours": "",
                "lines-0-day_1_inventory": "",
                "lines-0-day_2_shift_1": "",
                "lines-0-day_2_shift_2": "",
                "lines-0-day_2_compensation_mode": "",
                "lines-0-day_2_compensation_hours": "",
                "lines-0-day_2_inventory": "",
                "lines-0-day_3_shift_1": "",
                "lines-0-day_3_shift_2": "",
                "lines-0-day_3_compensation_mode": "",
                "lines-0-day_3_compensation_hours": "",
                "lines-0-day_3_inventory": "",
                "lines-0-day_4_shift_1": "",
                "lines-0-day_4_shift_2": "",
                "lines-0-day_4_compensation_mode": "",
                "lines-0-day_4_compensation_hours": "",
                "lines-0-day_4_inventory": "",
                "lines-0-day_5_shift_1": "",
                "lines-0-day_5_shift_2": "",
                "lines-0-day_5_compensation_mode": "",
                "lines-0-day_5_compensation_hours": "",
                "lines-0-day_5_inventory": "",
                "lines-0-day_6_shift_1": "",
                "lines-0-day_6_shift_2": "",
                "lines-0-day_6_compensation_mode": "",
                "lines-0-day_6_compensation_hours": "",
                "lines-0-day_6_inventory": "",
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(save_response.status_code, 302)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.notes, "Revisado por admin")
        self.assertFalse(self.schedule.admin_edit_enabled)
        self.assertTrue(self.schedule.is_closed)

    def test_autosave_keeps_reopened_published_schedule_editable(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.admin_edit_enabled = True
        self.schedule.save()
        self.client.login(username="admin_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            self.build_schedule_form_payload(
                status=WeeklySchedule.Status.PUBLISHED,
                notes="Autoguardado en horario reabierto",
            ),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_X_SCHEDULE_AUTOSAVE="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.notes, "Autoguardado en horario reabierto")
        self.assertTrue(self.schedule.admin_edit_enabled)
        self.assertFalse(self.schedule.is_closed)

    def test_site_user_cannot_unlock_published_schedule(self):
        self.schedule.status = WeeklySchedule.Status.PUBLISHED
        self.schedule.save()
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:unlock", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 403)

    @patch("schedules.forms.lookup_third_party_by_identifier", return_value=None)
    def test_personal_vario_manual_add_requires_blacklisted_identifier(self, _mock_lookup):
        schedule = WeeklySchedule.objects.create(
            site=self.personal_vario,
            week_start_date=date(2026, 6, 14),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        self.client.login(username="admin_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": schedule.pk}),
            {
                "manual_add_submit": "1",
                "manual-lookup_attempts": "2",
                "manual-employee_identifier": "999888777",
                "manual-employee_name": "No Bloqueado",
                "manual-job_role": str(self.job_role.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "lista negra")
        self.assertFalse(
            ScheduleLine.objects.filter(schedule=schedule, employee_identifier="999888777").exists()
        )

    def test_schedule_excel_download_returns_spreadsheet(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:excel-download", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(".xlsx", response["Content-Disposition"])
        workbook = load_workbook(BytesIO(response.content))
        worksheet = workbook.active
        self.assertEqual(worksheet["A6"].value, "Cedula")
        self.assertEqual(worksheet["D6"].value, "Domingo\n07/06/2026")
        self.assertEqual(worksheet["D7"].value, "Turno 1")
        self.assertEqual(worksheet["E7"].value, "Turno 2")
        self.assertEqual(worksheet["F7"].value, "Horas")
        self.assertEqual(worksheet["A8"].value, self.line.employee_identifier)


class ScheduleLoadViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.site = Site.objects.create(code="007", name="JARDIN.I")
        self.admin_user = User.objects.create_user(username="admin_load", password="secret")
        admin_access = UserSiteAccess.objects.get(user=self.admin_user)
        admin_access.role = UserSiteAccess.Role.ADMIN
        admin_access.save()

    @patch("schedules.services.fetch_active_staff_for_site")
    def test_schedule_load_rebuilds_future_balance_automatically(self, mock_fetch_staff):
        EmployeeInitialBalance.objects.create(
            employee_identifier="7100",
            employee_name="Empleado Creacion",
            initial_day_balance=Decimal("4.00"),
        )
        template_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 7, 19),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        ScheduleLine.objects.create(
            schedule=template_schedule,
            employee_identifier="7100",
            employee_name="Empleado Creacion",
            job_role_name="AUXILIAR",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="08:00-16:00",
        )
        future_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 7, 12),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        future_line = ScheduleLine.objects.create(
            schedule=future_schedule,
            employee_identifier="7100",
            employee_name="Empleado Creacion",
            job_role_name="AUXILIAR",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
        )
        rebuild_balances_for_employees_from_week(future_schedule.week_start_date, ["7100"])
        future_line.refresh_from_db()
        self.assertEqual(future_line.accrued_day_balance, Decimal("4.00"))

        mock_fetch_staff.return_value = [
            OperationalStaffingRecord(
                employee_id="7100",
                employee_name="Empleado Creacion",
                site_code=self.site.code,
                department_code="A1",
                department_name="CARNES",
                role_code="AUX",
                role_name="AUXILIAR",
            ),
        ]

        self.client.login(username="admin_load", password="secret")
        response = self.client.post(
            reverse("schedules:load"),
            {
                "site": str(self.site.pk),
                "week_start_date": "2026-07-05",
                "copy_from_schedule": str(template_schedule.pk),
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        created_schedule = WeeklySchedule.objects.get(site=self.site, week_start_date=date(2026, 7, 5))
        created_line = ScheduleLine.objects.get(schedule=created_schedule, employee_identifier="7100")
        future_line.refresh_from_db()

        self.assertEqual(created_line.day_0_shift_1, "08:00-16:00")
        self.assertEqual(created_line.accrued_day_balance, Decimal("5.00"))
        self.assertEqual(future_line.accrued_day_balance, Decimal("5.00"))


class InitialBalanceUploadViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.site = Site.objects.create(code="007", name="JARDIN.I")
        self.schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 7, 5),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        self.line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="999",
            employee_name="Empleado Base",
            weekly_target_hours=Decimal("36.00"),
            daily_max_hours=Decimal("8.00"),
            day_1_shift_1="18:00-00:00",
            day_2_shift_1="18:00-00:00",
            day_3_shift_1="18:00-00:00",
            day_4_shift_1="18:00-00:00",
            day_5_shift_1="18:00-00:00",
            day_6_shift_1="18:00-00:00",
        )
        self.admin_user = User.objects.create_user(username="admin_balances", password="secret")
        admin_access = UserSiteAccess.objects.get(user=self.admin_user)
        admin_access.role = UserSiteAccess.Role.ADMIN
        admin_access.save()
        self.site_user = User.objects.create_user(username="site_balances", password="secret")
        site_access = UserSiteAccess.objects.get(user=self.site_user)
        site_access.sites.add(self.site)

    def build_upload_file(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["Cedula", "Nombres y apellidos", "Dias extras", "Horas extras"])
        worksheet.append(["999", "Empleado Base", 1, 2.5])
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        return output

    def build_csv_upload_file(self):
        content = (
            "Cedula,Nombres y apellidos,Dias extras,Horas extras\n"
            "999,Empleado Base,1,2.5\n"
        )
        return SimpleUploadedFile(
            "saldos_iniciales.csv",
            content.encode("utf-8"),
            content_type="text/csv",
        )

    def test_site_user_cannot_access_initial_balance_loader(self):
        self.client.login(username="site_balances", password="secret")

        response = self.client.get(
            reverse("schedules:initial-balances"),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 403)

    def test_admin_can_upload_initial_balances_and_rebuild_history(self):
        recalculate_schedule_line(self.line)
        self.line.save()
        self.assertEqual(self.line.accrued_day_balance, Decimal("0.00"))
        self.assertEqual(self.line.accrued_hour_balance, Decimal("0.00"))

        self.client.login(username="admin_balances", password="secret")
        upload = self.build_upload_file()
        upload.name = "saldos_iniciales.xlsx"

        response = self.client.post(
            reverse("schedules:initial-balances"),
            {
                "balances-file": upload,
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        balance = EmployeeInitialBalance.objects.get(employee_identifier="999")
        self.assertEqual(balance.initial_day_balance, Decimal("1.00"))
        self.assertEqual(balance.initial_hour_balance, Decimal("2.50"))

        self.line.refresh_from_db()
        self.assertEqual(self.line.accrued_day_balance, Decimal("1.00"))
        self.assertEqual(self.line.accrued_hour_balance, Decimal("2.50"))

    def test_admin_can_upload_initial_balances_from_csv(self):
        recalculate_schedule_line(self.line)
        self.line.save()

        self.client.login(username="admin_balances", password="secret")
        upload = self.build_csv_upload_file()

        response = self.client.post(
            reverse("schedules:initial-balances"),
            {
                "balances-file": upload,
            },
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 302)
        balance = EmployeeInitialBalance.objects.get(employee_identifier="999")
        self.assertEqual(balance.initial_day_balance, Decimal("1.00"))
        self.assertEqual(balance.initial_hour_balance, Decimal("2.50"))


class InitialBalanceAuditTests(TestCase):
    def setUp(self):
        config = SystemConfiguration.load()
        config.default_weekly_hours = Decimal("46.00")
        config.default_daily_max_hours = Decimal("8.00")
        config.save()

        self.site = Site.objects.create(code="007", name="JARDIN.I")
        self.first_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 6, 28),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        self.second_schedule = WeeklySchedule.objects.create(
            site=self.site,
            week_start_date=date(2026, 7, 5),
            first_day_index=SystemConfiguration.SUNDAY,
        )
        self.first_line = ScheduleLine.objects.create(
            schedule=self.first_schedule,
            employee_identifier="9100",
            employee_name="Empleado Auditoria",
            job_role_name="AUXILIAR DE CARNES",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="08:00-16:00",
            day_1_shift_1="08:00-16:00",
            day_2_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            day_3_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
        )
        self.second_line = ScheduleLine.objects.create(
            schedule=self.second_schedule,
            employee_identifier="9100",
            employee_name="Empleado Auditoria",
            job_role_name="AUXILIAR DE CARNES",
            weekly_target_hours=Decimal("46.00"),
            daily_max_hours=Decimal("8.00"),
        )

    def test_initial_balance_update_rebuilds_existing_weeks_and_keeps_audit_in_sync(self):
        balance = EmployeeInitialBalance.objects.create(
            employee_identifier="9100",
            employee_name="Empleado Auditoria",
            initial_day_balance=Decimal("9.00"),
            initial_hour_balance=Decimal("0.00"),
        )

        self.first_line.refresh_from_db()
        self.second_line.refresh_from_db()
        self.assertEqual(self.first_line.accrued_day_balance, Decimal("9.00"))
        self.assertEqual(self.second_line.accrued_day_balance, Decimal("9.00"))

        audit_row = build_schedule_balance_audit_rows(["9100"])[0]
        self.assertEqual(audit_row["audited_day_balance"], Decimal("9.00"))
        self.assertEqual(audit_row["stored_day_balance"], Decimal("9.00"))
        self.assertFalse(audit_row["has_difference"])

        balance.initial_day_balance = Decimal("7.00")
        balance.save()

        self.first_line.refresh_from_db()
        self.second_line.refresh_from_db()
        self.assertEqual(self.first_line.accrued_day_balance, Decimal("7.00"))
        self.assertEqual(self.second_line.accrued_day_balance, Decimal("7.00"))

        audit_row = build_schedule_balance_audit_rows(["9100"])[0]
        self.assertEqual(audit_row["audited_day_balance"], Decimal("7.00"))
        self.assertEqual(audit_row["stored_day_balance"], Decimal("7.00"))
        self.assertFalse(audit_row["has_difference"])

    def test_initial_balance_delete_rebuilds_existing_weeks(self):
        balance = EmployeeInitialBalance.objects.create(
            employee_identifier="9100",
            employee_name="Empleado Auditoria",
            initial_day_balance=Decimal("9.00"),
        )
        self.first_line.refresh_from_db()
        self.second_line.refresh_from_db()
        self.assertEqual(self.second_line.accrued_day_balance, Decimal("9.00"))

        balance.delete()

        self.first_line.refresh_from_db()
        self.second_line.refresh_from_db()
        self.assertEqual(self.first_line.accrued_day_balance, Decimal("0.00"))
        self.assertEqual(self.second_line.accrued_day_balance, Decimal("0.00"))

        audit_row = build_schedule_balance_audit_rows(["9100"])[0]
        self.assertEqual(audit_row["audited_day_balance"], Decimal("0.00"))
        self.assertEqual(audit_row["stored_day_balance"], Decimal("0.00"))
        self.assertFalse(audit_row["has_difference"])

    @patch("schedules.services.rebuild_balances_for_employees_from_week")
    def test_import_employee_initial_balances_rebuilds_history_once(self, rebuild_mock):
        upload = SimpleUploadedFile(
            "saldos_iniciales.csv",
            "Cedula,Nombres y apellidos,Dias extras,Horas extras\n9100,Empleado Auditoria,9,0\n".encode("utf-8"),
            content_type="text/csv",
        )

        result = import_employee_initial_balances(upload)

        self.assertEqual(result["processed_count"], 1)
        rebuild_mock.assert_called_once()

    def test_audit_command_detects_and_fixes_mismatch(self):
        EmployeeInitialBalance.objects.create(
            employee_identifier="9100",
            employee_name="Empleado Auditoria",
            initial_day_balance=Decimal("9.00"),
        )
        self.first_line.refresh_from_db()
        self.second_line.refresh_from_db()
        self.assertEqual(self.second_line.accrued_day_balance, Decimal("9.00"))

        EmployeeInitialBalance.objects.filter(employee_identifier="9100").update(
            initial_day_balance=Decimal("8.00")
        )

        output = StringIO()
        call_command("audit_schedule_balances", stdout=output)
        command_output = output.getvalue()
        self.assertIn("9100", command_output)
        self.assertIn("dif_dias=1.00", command_output)

        fixed_output = StringIO()
        call_command("audit_schedule_balances", "--fix", stdout=fixed_output)
        self.second_line.refresh_from_db()
        self.assertEqual(self.second_line.accrued_day_balance, Decimal("8.00"))

        audit_row = build_schedule_balance_audit_rows(["9100"])[0]
        self.assertFalse(audit_row["has_difference"])
        self.assertIn("Recalculo aplicado correctamente", fixed_output.getvalue())
