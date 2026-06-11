from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import JobRole, ShiftTemplate, Site, SystemConfiguration, UserSiteAccess
from schedules.forms import ScheduleLineForm
from schedules.models import ScheduleLine, WeeklySchedule
from schedules.services import (
    get_rest_shift_label,
    get_schedule_line_compact_alert_summary,
    parse_shift_hours,
    recalculate_schedule_line,
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
            week_start_date=date(2026, 6, 7),
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
        data = {
            "pending_dates_note": "",
            "pending_days": "0",
            "pending_hours": "0",
        }
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
            pending_hours=Decimal("1.00"),
        )

        recalculate_schedule_line(line)

        self.assertEqual(line.day_0_hours, Decimal("8.00"))
        self.assertEqual(line.total_hours, Decimal("8.00"))
        self.assertEqual(line.overtime_hours, Decimal("0.00"))
        self.assertEqual(line.pending_hours_variance, Decimal("1.00"))

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
            pending_days=Decimal("2.00"),
            pending_hours=Decimal("3.00"),
            overtime_hours=Decimal("4.00"),
            payment_days_used=1,
            payment_hours_used=Decimal("1.50"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="128",
            employee_name="Empleado Saldo",
            weekly_target_hours=Decimal("4.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="08:00-16:00",
            day_1_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            pending_days=Decimal("1.00"),
            pending_hours=Decimal("2.00"),
        )

        recalculate_schedule_line(line)

        self.assertEqual(line.overtime_hours, Decimal("4.00"))
        self.assertEqual(line.payment_days_used, 1)
        self.assertEqual(line.payment_hours_used, Decimal("0.00"))
        self.assertEqual(line.pending_hours_variance, Decimal("19.50"))

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

    def test_compact_alert_summary_groups_main_categories(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="133",
            employee_name="Empleado Resumen",
            weekly_target_hours=Decimal("4.00"),
            daily_max_hours=Decimal("8.00"),
            day_0_shift_1="08:00-16:00",
            pending_dates_note="2026-06-20,2026-06-21",
            pending_days=Decimal("1.00"),
        )

        recalculate_schedule_line(line)

        compact_summary = get_schedule_line_compact_alert_summary(line)
        self.assertIn("horas semanales", compact_summary.lower())
        self.assertIn("pendientes", compact_summary.lower())

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

    def test_form_accepts_pending_dates_outside_week(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="125",
            employee_name="Empleado Demo 3",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                pending_dates_note="2026-06-20",
                pending_days="1",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_form_rejects_decimal_pending_days(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="127",
            employee_name="Empleado Demo 4",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(pending_days="1.5"),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("entero", form.errors["pending_days"][0].lower())

    def test_form_rejects_pending_dates_mismatch(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="129",
            employee_name="Empleado Demo 5",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                pending_dates_note="2026-06-10,2026-06-11",
                pending_days="1",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("coincidir", form.errors["pending_dates_note"][0].lower())
        self.assertIn("coincidir", form.errors["pending_days"][0].lower())

    def test_form_rejects_pay_day_without_prior_day_balance(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="130",
            employee_name="Empleado Demo 6",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(day_0_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("horarios anteriores", form.errors["day_0_compensation_mode"][0].lower())

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
            pending_days=Decimal("1.00"),
            daily_max_hours=Decimal("8.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="131",
            employee_name="Empleado Demo 7",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="08:00-16:00",
                day_0_compensation_mode=ScheduleLine.CompensationMode.PAY_DAY,
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["day_0_shift_1"], get_rest_shift_label())
        self.assertEqual(form.cleaned_data["day_0_shift_2"], "")

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
            pending_hours=Decimal("1.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="132",
            employee_name="Empleado Demo 8",
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                day_0_compensation_hours="1.5",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("saldo previo disponible", form.errors["day_0_compensation_hours"][0].lower())

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
            pending_hours=Decimal("5.00"),
            daily_max_hours=Decimal("8.00"),
        )
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="134",
            employee_name="Empleado Demo 9",
            daily_max_hours=Decimal("8.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="06:00-10:00",
                day_0_compensation_mode=ScheduleLine.CompensationMode.PAY_HOURS,
                day_0_compensation_hours="5",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("completar la jornada", form.errors["day_0_compensation_hours"][0].lower())

    def test_form_rejects_second_shift_when_first_shift_already_meets_daily_journey(self):
        line = ScheduleLine.objects.create(
            schedule=self.schedule,
            employee_identifier="135",
            employee_name="Empleado Demo 10",
            daily_max_hours=Decimal("4.00"),
        )
        form = ScheduleLineForm(
            data=self.build_form_data(
                day_0_shift_1="06:00-10:00",
                day_0_shift_2="13:00-17:00",
            ),
            instance=line,
            schedule=self.schedule,
            shift_choices=[],
            secondary_shift_choices=[],
        )

        self.assertFalse(form.is_valid())
        self.assertIn("cumple la jornada", form.errors["day_0_shift_2"][0].lower())

    @patch("schedules.services.fetch_active_staff_for_site", return_value=[])
    def test_sync_schedule_uses_schedule_week_start_for_legacy_period(self, mock_fetch_staff):
        created_count, updated_count = sync_schedule_from_legacy(self.schedule)

        self.assertEqual(created_count, 0)
        self.assertEqual(updated_count, 0)
        mock_fetch_staff.assert_called_once_with(
            self.site.code,
            week_start_date=self.schedule.week_start_date,
        )


class ScheduleDeleteViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.site = Site.objects.create(code="007", name="JARDIN.I")
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
            pending_dates_note="2026-06-10,2026-06-11",
            pending_days=Decimal("1.00"),
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

    def test_admin_sees_delete_action(self):
        self.client.login(username="admin_delete", password="secret")

        response = self.client.get(
            reverse("schedules:list"),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "plain-action-button")

    def test_site_user_can_add_manual_schedule_line(self):
        self.client.login(username="operador_delete", password="secret")

        response = self.client.post(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            {
                "manual_add_submit": "1",
                "manual-employee_document_type": "CC",
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
        self.assertEqual(manual_line.employee_document_type, "CC")
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
                "manual-employee_document_type": "CC",
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
        self.client.login(username="operador_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revisa horas semanales, pendientes.")
        self.assertNotContains(response, "Supera el objetivo semanal")

    def test_admin_sees_detailed_alert_summary(self):
        self.client.login(username="admin_delete", password="secret")

        response = self.client.get(
            reverse("schedules:edit", kwargs={"pk": self.schedule.pk}),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Supera el objetivo semanal")

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
                "lines-0-pending_dates_note": self.line.pending_dates_note,
                "lines-0-pending_days": "1",
                "lines-0-pending_hours": "0",
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
                "manual-employee_document_type": "CC",
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
