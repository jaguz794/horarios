from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client
from django.test import TestCase
from django.urls import reverse

from core.access import get_accessible_sites_queryset, user_can_manage_all_sites
from core.models import Site, UserSiteAccess
from schedules.models import ScheduleLine, WeeklySchedule

User = get_user_model()


class UserSiteAccessTests(TestCase):
    def setUp(self):
        self.site_a = Site.objects.create(code="007", name="JARDIN.I")
        self.site_b = Site.objects.create(code="008", name="SALADO")

    def test_site_user_only_sees_assigned_sites(self):
        user = User.objects.create_user(username="operador", password="secret")
        access = UserSiteAccess.objects.get(user=user)
        access.sites.add(self.site_a)

        visible_codes = list(
            get_accessible_sites_queryset(user).order_by("code").values_list("code", flat=True)
        )

        self.assertEqual(visible_codes, ["007"])
        self.assertFalse(user_can_manage_all_sites(user))

    def test_admin_role_can_manage_all_sites(self):
        user = User.objects.create_user(username="admin_local", password="secret")
        access = UserSiteAccess.objects.get(user=user)
        access.role = UserSiteAccess.Role.ADMIN
        access.save()

        visible_codes = list(
            get_accessible_sites_queryset(user).order_by("code").values_list("code", flat=True)
        )

        self.assertEqual(visible_codes, ["007", "008"])
        self.assertTrue(user_can_manage_all_sites(user))

    def test_user_without_assigned_sites_sees_none(self):
        user = User.objects.create_user(username="sin_sede", password="secret")

        visible_codes = list(
            get_accessible_sites_queryset(user).order_by("code").values_list("code", flat=True)
        )

        self.assertEqual(visible_codes, [])


class DashboardViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.site_a = Site.objects.create(code="007", name="JARDIN.I")
        self.site_b = Site.objects.create(code="008", name="SALADO")
        self.user = User.objects.create_user(username="admin_dashboard", password="secret")
        access = UserSiteAccess.objects.get(user=self.user)
        access.role = UserSiteAccess.Role.ADMIN
        access.save()
        self.schedule_a = WeeklySchedule.objects.create(
            site=self.site_a,
            week_start_date=date(2026, 6, 7),
            status=WeeklySchedule.Status.DRAFT,
        )
        self.schedule_b = WeeklySchedule.objects.create(
            site=self.site_b,
            week_start_date=date(2026, 6, 14),
            status=WeeklySchedule.Status.PUBLISHED,
        )
        ScheduleLine.objects.create(
            schedule=self.schedule_a,
            employee_identifier="100",
            employee_name="Ana",
            job_role_name="AUXILIAR",
            weekly_target_hours=Decimal("8.00"),
            total_hours=Decimal("10.00"),
            warnings_count=2,
        )
        ScheduleLine.objects.create(
            schedule=self.schedule_a,
            employee_identifier="101",
            employee_name="Bruno",
            job_role_name="CAJERO",
            warnings_count=0,
        )
        ScheduleLine.objects.create(
            schedule=self.schedule_b,
            employee_identifier="102",
            employee_name="Carla",
            job_role_name="SUPERVISOR",
            pending_dates_note="2026-06-20",
            pending_days=Decimal("0.00"),
            warnings_count=1,
        )

    def test_dashboard_shows_alert_tooltip_and_schedule_summaries(self):
        self.client.login(username="admin_dashboard", password="secret")

        response = self.client.get(
            reverse("dashboard"),
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Resumen por horario")
        self.assertNotContains(response, "3 horario(s) con novedades")
        self.assertContains(response, "2 horario(s) con novedades")
        self.assertContains(response, "2 persona(s) cargadas.")
        self.assertContains(response, "2 alerta(s) en 1 colaborador(es).")
        self.assertContains(response, "Revisa horas semanales.")
        self.assertContains(response, "Revisa pendientes.")
