from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.views.generic import ListView, TemplateView

from core.access import get_accessible_schedules_queryset, get_accessible_sites_queryset
from core.models import Site, SystemConfiguration
from schedules.models import WeeklySchedule
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
            }
        )
        return context


class SiteListView(LoginRequiredMixin, ListView):
    model = Site
    template_name = "core/site_list.html"
    context_object_name = "sites"

    def get_queryset(self):
        queryset = Site.objects.filter(is_active=True).annotate(schedule_count=Count("weekly_schedules"))
        return get_accessible_sites_queryset(self.request.user, queryset)
