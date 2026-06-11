from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import FormView, ListView, TemplateView

from core.access import get_accessible_schedules_queryset, user_can_delete_schedules, user_can_manage_all_sites
from core.models import SystemConfiguration
from schedules.forms import (
    ScheduleLineFormSet,
    ScheduleLineManualAddForm,
    ScheduleLoadForm,
    WeeklyScheduleForm,
    build_shift_choices,
)
from schedules.models import WeeklySchedule
from schedules.services import build_shift_metrics_catalog, recalculate_schedule_line, sync_schedule_from_legacy


class ScheduleListView(LoginRequiredMixin, ListView):
    model = WeeklySchedule
    template_name = "schedules/schedule_list.html"
    context_object_name = "schedules"

    def get_queryset(self):
        queryset = WeeklySchedule.objects.select_related("site").order_by("-week_start_date", "site__code")
        return get_accessible_schedules_queryset(self.request.user, queryset)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_delete_schedules"] = user_can_delete_schedules(self.request.user)
        return context


class ScheduleLoadView(LoginRequiredMixin, FormView):
    template_name = "schedules/schedule_load.html"
    form_class = ScheduleLoadForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        site_id = self.request.GET.get("site")
        if site_id:
            initial["site"] = site_id
        return initial

    def form_valid(self, form):
        config = SystemConfiguration.load()
        site = form.cleaned_data["site"]
        week_start_date = form.cleaned_data["week_start_date"]
        schedule, created = WeeklySchedule.objects.get_or_create(
            site=site,
            week_start_date=week_start_date,
            defaults={
                "first_day_index": config.week_start_day,
                "created_by": self.request.user,
                "updated_by": self.request.user,
            },
        )
        if not created and not schedule.is_closed:
            schedule.updated_by = self.request.user
            schedule.save(update_fields=["updated_by", "updated_at"])
        if not created and schedule.is_closed:
            messages.info(self.request, "El horario ya esta publicado y se abrio en modo cerrado.")
            return redirect("schedules:edit", pk=schedule.pk)

        if created:
            created_count, updated_count = sync_schedule_from_legacy(schedule)
            messages.success(
                self.request,
                f"Horario cargado. Nuevos: {created_count}. Actualizados: {updated_count}.",
            )
        else:
            messages.info(self.request, "Se abrio el horario existente sin recargar personal.")

        return redirect("schedules:edit", pk=schedule.pk)


class ScheduleEditView(LoginRequiredMixin, TemplateView):
    template_name = "schedules/schedule_edit.html"

    def get_manual_add_form(self, schedule, data=None, readonly: bool = False):
        return ScheduleLineManualAddForm(
            data=data,
            schedule=schedule,
            prefix="manual",
            readonly=readonly,
        )

    def get_line_form_kwargs(self, schedule, readonly: bool = False):
        return {
            "schedule": schedule,
            "shift_choices": build_shift_choices(second_slot=False),
            "secondary_shift_choices": build_shift_choices(second_slot=True),
            "readonly": readonly,
        }

    def get_schedule(self):
        queryset = WeeklySchedule.objects.select_related("site")
        queryset = get_accessible_schedules_queryset(self.request.user, queryset)
        return get_object_or_404(queryset, pk=self.kwargs["pk"])

    def get(self, request, *args, **kwargs):
        schedule = self.get_schedule()
        return self.render_to_response(self.build_context(schedule))

    def post(self, request, *args, **kwargs):
        schedule = self.get_schedule()
        if schedule.is_closed:
            messages.error(request, "El horario publicado esta cerrado y ya no admite modificaciones.")
            return redirect("schedules:edit", pk=schedule.pk)
        if "manual_add_submit" in request.POST:
            manual_add_form = self.get_manual_add_form(schedule, data=request.POST, readonly=False)
            if manual_add_form.is_valid():
                line = manual_add_form.save()
                messages.success(
                    request,
                    f"Trabajador agregado manualmente al horario: {line.employee_name}.",
                )
                return redirect("schedules:edit", pk=schedule.pk)

            messages.error(request, "Revisa los datos del trabajador manual antes de agregarlo.")
            return self.render_to_response(
                self.build_context(
                    schedule,
                    manual_add_form=manual_add_form,
                )
            )

        schedule_form = WeeklyScheduleForm(request.POST, instance=schedule)
        line_formset = ScheduleLineFormSet(
            request.POST,
            instance=schedule,
            form_kwargs=self.get_line_form_kwargs(schedule, readonly=False),
        )

        if schedule_form.is_valid() and line_formset.is_valid():
            with transaction.atomic():
                updated_schedule = schedule_form.save(commit=False)
                updated_schedule.updated_by = request.user
                updated_schedule.save()
                line_formset.save()
                for line in updated_schedule.lines.all():
                    recalculate_schedule_line(line)
                    line.save()

            messages.success(request, "Horario actualizado correctamente.")
            return redirect("schedules:edit", pk=schedule.pk)

        messages.error(request, "Hay campos por revisar antes de guardar.")
        return self.render_to_response(
            self.build_context(
                schedule,
                schedule_form=schedule_form,
                line_formset=line_formset,
            )
        )

    def build_context(self, schedule, schedule_form=None, line_formset=None, manual_add_form=None):
        config = SystemConfiguration.load()
        schedule_closed = schedule.is_closed
        schedule_form = schedule_form or WeeklyScheduleForm(instance=schedule, readonly=schedule_closed)
        manual_add_form = manual_add_form or self.get_manual_add_form(schedule, readonly=schedule_closed)
        line_formset = line_formset or ScheduleLineFormSet(
            instance=schedule,
            form_kwargs=self.get_line_form_kwargs(schedule, readonly=schedule_closed),
        )
        return {
            "schedule": schedule,
            "schedule_form": schedule_form,
            "manual_add_form": manual_add_form,
            "line_formset": line_formset,
            "day_columns": schedule.get_day_columns(),
            "config": config,
            "shift_metrics": build_shift_metrics_catalog(config=config),
            "can_delete_schedules": user_can_delete_schedules(self.request.user),
            "show_night_hours": user_can_manage_all_sites(self.request.user),
            "show_detailed_alerts": user_can_manage_all_sites(self.request.user),
            "schedule_closed": schedule_closed,
        }


class ScheduleRefreshView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        queryset = get_accessible_schedules_queryset(request.user, WeeklySchedule.objects.all())
        schedule = get_object_or_404(queryset, pk=self.kwargs["pk"])
        if schedule.is_closed:
            messages.error(request, "El horario publicado esta cerrado y no se puede recargar personal.")
            return redirect(reverse("schedules:edit", kwargs={"pk": schedule.pk}))
        created_count, updated_count = sync_schedule_from_legacy(schedule)
        messages.success(
            request,
            f"Personal actualizado. Nuevos: {created_count}. Actualizados: {updated_count}.",
        )
        return redirect(reverse("schedules:edit", kwargs={"pk": schedule.pk}))


class ScheduleDeleteView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not user_can_delete_schedules(request.user):
            raise PermissionDenied("Solo el perfil administrador puede eliminar horarios.")
        queryset = get_accessible_schedules_queryset(request.user, WeeklySchedule.objects.prefetch_related("lines"))
        schedule = get_object_or_404(queryset, pk=self.kwargs["pk"])
        site_name = schedule.site.name
        week_start = schedule.week_start_date
        line_count = schedule.lines.count()
        schedule.delete()
        messages.success(
            request,
            f"Horario eliminado: {site_name} - {week_start}. Se borraron {line_count} registros de personal.",
        )
        return redirect("schedules:list")
