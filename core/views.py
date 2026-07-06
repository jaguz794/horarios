from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.views.generic import ListView, TemplateView

from core.access import get_accessible_schedules_queryset, get_accessible_sites_queryset, user_can_manage_all_sites
from core.models import Site, SystemConfiguration
from schedules.forms import ReportRangeForm, WeeklyBalanceReportForm
from schedules.inventory_pdf import build_inventory_week_pdf_bytes, get_inventory_report_filename
from schedules.models import WeeklySchedule
from schedules.reporting import (
    build_balance_role_breakdown,
    build_excel_response,
    build_inventory_report_rows,
    build_night_bonus_report_rows,
    build_overtime_balance_report_rows,
    build_special_days_report_rows,
    build_weekly_balance_report_rows,
    get_accessible_schedule_queryset_for_range,
    get_latest_visible_lines_by_employee,
    get_weekly_balance_lines,
)
from schedules.services import get_schedule_line_compact_alert_summary


def build_schedule_dashboard_summary(schedule: WeeklySchedule) -> dict[str, int | str]:
    lines = list(schedule.lines.all())
    line_count = len(lines)
    warning_total = sum(line.warnings_count or 0 for line in lines)
    warning_line_count = sum(1 for line in lines if (line.warnings_count or 0) > 0)
    unique_alert_summaries: list[str] = []

    for line in lines:
        if not (line.warnings_count or 0):
            continue
        compact_summary = get_schedule_line_compact_alert_summary(line)
        if compact_summary and compact_summary not in unique_alert_summaries:
            unique_alert_summaries.append(compact_summary)

    summary_parts = unique_alert_summaries[:2]
    if len(unique_alert_summaries) > 2:
        summary_parts.append(f"+{len(unique_alert_summaries) - 2} mas")

    return {
        "line_count": line_count,
        "warning_total": warning_total,
        "warning_line_count": warning_line_count,
        "week_label": f"{schedule.week_start_date:%d/%m/%Y} a {schedule.week_end_date:%d/%m/%Y}",
        "summary_text": " | ".join(summary_parts) if summary_parts else "Sin alertas activas.",
    }


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = SystemConfiguration.load()
        accessible_sites = get_accessible_sites_queryset(
            self.request.user,
            Site.objects.filter(is_active=True),
        )
        dashboard_schedules = list(
            get_accessible_schedules_queryset(
                self.request.user,
                WeeklySchedule.objects.select_related("site").prefetch_related("lines"),
            ).order_by("-week_start_date")
        )
        schedule_count = len(dashboard_schedules)
        draft_count = sum(1 for schedule in dashboard_schedules if schedule.status == WeeklySchedule.Status.DRAFT)
        review_count = sum(1 for schedule in dashboard_schedules if schedule.status == WeeklySchedule.Status.REVIEW)
        published_count = sum(1 for schedule in dashboard_schedules if schedule.status == WeeklySchedule.Status.PUBLISHED)
        warning_count = 0
        warning_schedule_count = 0
        warning_line_count = 0
        dashboard_alert_items: list[dict[str, int | str]] = []

        for schedule in dashboard_schedules:
            summary = build_schedule_dashboard_summary(schedule)
            schedule.dashboard_line_count = summary["line_count"]
            schedule.dashboard_warning_total = summary["warning_total"]
            schedule.dashboard_warning_line_count = summary["warning_line_count"]
            schedule.dashboard_summary_text = summary["summary_text"]
            schedule.dashboard_week_label = summary["week_label"]
            warning_count += int(summary["warning_total"])
            warning_line_count += int(summary["warning_line_count"])
            if summary["warning_total"]:
                warning_schedule_count += 1
                dashboard_alert_items.append(
                    {
                        "site_name": schedule.site.name,
                        "week_label": summary["week_label"],
                        "warning_total": summary["warning_total"],
                        "warning_line_count": summary["warning_line_count"],
                        "summary_text": summary["summary_text"],
                    }
                )

        recent_schedules = dashboard_schedules[:6]
        balance_breakdown = build_balance_role_breakdown(get_latest_visible_lines_by_employee(self.request.user))
        context.update(
            {
                "config": config,
                "site_count": accessible_sites.count(),
                "schedule_count": schedule_count,
                "draft_count": draft_count,
                "review_count": review_count,
                "published_count": published_count,
                "warning_count": warning_count,
                "warning_schedule_count": warning_schedule_count,
                "warning_line_count": warning_line_count,
                "recent_schedules": recent_schedules,
                "dashboard_alert_items": dashboard_alert_items,
                "extra_day_total": balance_breakdown["total_days"],
                "extra_hour_total": balance_breakdown["total_hours"],
                "extra_day_role_items": balance_breakdown["days_by_role"],
                "extra_hour_role_items": balance_breakdown["hours_by_role"],
            }
        )
        return context


class ReportHubView(LoginRequiredMixin, TemplateView):
    template_name = "core/reports.html"

    def get_range_form(self, data=None):
        return ReportRangeForm(data=data, user=self.request.user, prefix="range")

    def get_weekly_form(self, data=None):
        return WeeklyBalanceReportForm(data=data, user=self.request.user, prefix="week")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("range_form", self.get_range_form())
        context.setdefault("weekly_form", self.get_weekly_form())
        context["is_admin_scope"] = user_can_manage_all_sites(self.request.user)
        return context

    def post(self, request, *args, **kwargs):
        report_type = request.POST.get("report_type", "")
        is_admin_scope = user_can_manage_all_sites(request.user)

        if report_type in {"special_days", "overtime_balance", "night_bonus"} and not is_admin_scope:
            raise PermissionDenied("Ese informe solo esta disponible para administracion.")

        range_form = self.get_range_form(request.POST)
        weekly_form = self.get_weekly_form(request.POST)

        if report_type in {"special_days", "overtime_balance", "night_bonus"}:
            if range_form.is_valid():
                site = range_form.cleaned_data.get("site")
                date_from = range_form.cleaned_data["date_from"]
                date_to = range_form.cleaned_data["date_to"]
                schedules = list(get_accessible_schedule_queryset_for_range(request.user, date_from, date_to, site=site))

                if report_type == "special_days":
                    rows = build_special_days_report_rows(schedules, date_from, date_to)
                    return build_excel_response(
                        "DomingosFestivos",
                        ["Fecha", "Sede", "Cedula", "Nombre", "Domingo o festivo"],
                        rows,
                        "domingos_festivos_laborados.xlsx",
                    )
                if report_type == "overtime_balance":
                    rows = build_overtime_balance_report_rows(request.user, date_from, date_to, site=site)
                    return build_excel_response(
                        "HorasExtras",
                        ["Fecha generacion", "Sede", "Cedula", "Nombre", "Cantidad de horas"],
                        rows,
                        "horas_extras_acumuladas.xlsx",
                    )
                rows = build_night_bonus_report_rows(schedules, date_from, date_to)
                return build_excel_response(
                    "RecargosNocturnos",
                    ["Fecha", "Sede", "Cedula", "Nombre", "Cantidad de horas recargo nocturno"],
                    rows,
                    "recargos_nocturnos.xlsx",
                )

        if report_type in {"weekly_balance", "inventory_excel", "inventory_pdf"}:
            if weekly_form.is_valid():
                site = weekly_form.cleaned_data.get("site")
                week_start_date = weekly_form.cleaned_data["week_start_date"]
                lines = get_weekly_balance_lines(request.user, week_start_date, site=site)
                if report_type == "weekly_balance":
                    rows = build_weekly_balance_report_rows(lines)
                    return build_excel_response(
                        "ConsolidadoSemanal",
                        ["Sede", "Cargo", "Cedula", "Nombre", "Saldo acumulado (h equivalentes)", "Recargos nocturnos"],
                        rows,
                        "consolidado_semanal_horarios.xlsx",
                    )
                if report_type == "inventory_excel":
                    rows = build_inventory_report_rows(lines)
                    return build_excel_response(
                        "InventarioSemanal",
                        ["Fecha inventario", "Mes", "Sede", "Cedula", "Nombre", "Cargo", "Valor"],
                        rows,
                        "inventario_semanal.xlsx",
                    )

                pdf_bytes = build_inventory_week_pdf_bytes(lines, week_start_date)
                response = HttpResponse(pdf_bytes, content_type="application/pdf")
                response["Content-Disposition"] = (
                    f'attachment; filename="{get_inventory_report_filename(week_start_date)}"'
                )
                return response

        return self.render_to_response(
            self.get_context_data(
                range_form=range_form,
                weekly_form=weekly_form,
            )
        )


class SiteListView(LoginRequiredMixin, ListView):
    model = Site
    template_name = "core/site_list.html"
    context_object_name = "sites"

    def get_queryset(self):
        queryset = Site.objects.filter(is_active=True).annotate(schedule_count=Count("weekly_schedules"))
        return get_accessible_sites_queryset(self.request.user, queryset).order_by("code")
