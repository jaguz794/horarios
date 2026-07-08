from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import FormView, ListView, TemplateView

from core.access import get_accessible_schedules_queryset, user_can_delete_schedules, user_can_manage_all_sites
from core.models import SystemConfiguration
from schedules.forms import (
    DOCUMENT_NUMBER_PATTERN,
    InitialBalanceUploadForm,
    ScheduleLineFormSet,
    ScheduleFilterForm,
    ScheduleLineManualAddForm,
    ScheduleLoadForm,
    ScheduleSettlementForm,
    WeeklyScheduleForm,
    build_shift_choices,
)
from schedules.models import EmployeeInitialBalance, ScheduleLine, WeeklySchedule
from schedules.reporting import build_initial_balance_template_response, build_schedule_excel_response
from schedules.services import (
    build_shift_metrics_catalog,
    copy_schedule_template,
    import_employee_initial_balances,
    is_employee_blacklisted,
    purge_blacklisted_lines_from_schedule,
    rebuild_balances_for_employees_from_week,
    schedule_accepts_blacklisted_staff,
    save_schedule_line_with_balances,
    sync_schedule_from_legacy,
)
from schedules.settlement_pdf import generate_and_store_schedule_settlement
from legacy.services import lookup_third_party_by_identifier


class ScheduleListView(LoginRequiredMixin, ListView):
    model = WeeklySchedule
    template_name = "schedules/schedule_list.html"
    context_object_name = "schedules"

    def get_filter_form(self):
        return ScheduleFilterForm(self.request.GET or None, user=self.request.user)

    def get_queryset(self):
        queryset = WeeklySchedule.objects.select_related("site").order_by("-week_start_date", "site__code")
        queryset = get_accessible_schedules_queryset(self.request.user, queryset)
        self.filter_form = self.get_filter_form()
        if self.filter_form.is_valid():
            site = self.filter_form.cleaned_data.get("site")
            status = self.filter_form.cleaned_data.get("status")
            week_start_date = self.filter_form.cleaned_data.get("week_start_date")
            if site:
                queryset = queryset.filter(site=site)
            if status:
                queryset = queryset.filter(status=status)
            if week_start_date:
                queryset = queryset.filter(week_start_date=week_start_date)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_delete_schedules"] = user_can_delete_schedules(self.request.user)
        context["filter_form"] = getattr(self, "filter_form", self.get_filter_form())
        context["show_filters"] = user_can_manage_all_sites(self.request.user)
        context["is_admin_scope"] = user_can_manage_all_sites(self.request.user)
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
        copy_from_schedule = form.cleaned_data.get("copy_from_schedule")
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
                f"Horario cargado. Nuevos: {created_count}. Actualizados: {updated_count}. "
                "Los saldos relacionados se recalcularon automaticamente.",
            )
        else:
            messages.info(self.request, "Se abrio el horario existente sin recargar personal.")

        if copy_from_schedule and not schedule.is_closed:
            copied_count, source_line_count = copy_schedule_template(copy_from_schedule, schedule)
            if copied_count:
                messages.success(
                    self.request,
                    f"Plantilla copiada desde la semana {copy_from_schedule.week_start_date:%d/%m/%Y}. "
                    f"Se actualizaron {copied_count} trabajador(es).",
                )
            else:
                messages.warning(
                    self.request,
                    f"La semana base tenia {source_line_count} registro(s), pero no hubo coincidencias para copiar en esta sede.",
                )

        return redirect("schedules:edit", pk=schedule.pk)


class ScheduleEditView(LoginRequiredMixin, TemplateView):
    template_name = "schedules/schedule_edit.html"

    def can_reopen_published_schedule(self, schedule) -> bool:
        return user_can_manage_all_sites(self.request.user) and schedule.status == WeeklySchedule.Status.PUBLISHED

    def get_manual_add_form(
        self,
        schedule,
        data=None,
        readonly: bool = False,
        manual_name_enabled: bool = False,
        lookup_found: bool = False,
    ):
        return ScheduleLineManualAddForm(
            data=data,
            schedule=schedule,
            prefix="manual",
            readonly=readonly,
            manual_name_enabled=manual_name_enabled,
            lookup_found=lookup_found,
        )

    def get_line_form_kwargs(self, schedule, readonly: bool = False):
        is_admin_scope = user_can_manage_all_sites(self.request.user)
        return {
            "schedule": schedule,
            "shift_choices": build_shift_choices(second_slot=False),
            "secondary_shift_choices": build_shift_choices(second_slot=True),
            "readonly": readonly,
            "allow_money_payment": is_admin_scope,
            "show_admin_fields": is_admin_scope,
        }

    def get_schedule(self):
        queryset = WeeklySchedule.objects.select_related("site")
        queryset = get_accessible_schedules_queryset(self.request.user, queryset)
        return get_object_or_404(queryset, pk=self.kwargs["pk"])

    def get(self, request, *args, **kwargs):
        schedule = self.get_schedule()
        if not schedule.is_closed:
            purge_blacklisted_lines_from_schedule(schedule)
            schedule.refresh_from_db()
        return self.render_to_response(self.build_context(schedule))

    def post(self, request, *args, **kwargs):
        schedule = self.get_schedule()
        if not schedule.is_closed:
            purge_blacklisted_lines_from_schedule(schedule)
            schedule.refresh_from_db()
        if schedule.is_closed:
            messages.error(request, "El horario publicado esta cerrado y ya no admite modificaciones.")
            return redirect("schedules:edit", pk=schedule.pk)
        remove_line_id = request.POST.get("remove_line_id", "").strip()
        if remove_line_id:
            line = get_object_or_404(ScheduleLine.objects.filter(schedule=schedule), pk=remove_line_id)
            employee_name = line.employee_name
            employee_identifier = line.employee_identifier
            line.delete()
            rebuild_balances_for_employees_from_week(schedule.week_start_date, [employee_identifier])
            messages.success(
                request,
                f"Trabajador retirado del horario: {employee_name}.",
            )
            return redirect("schedules:edit", pk=schedule.pk)
        if "manual_lookup_submit" in request.POST:
            attempts = int(request.POST.get("manual-lookup_attempts", "0") or "0")
            next_attempts = attempts + 1
            identifier = (request.POST.get("manual-employee_identifier", "") or "").strip().upper()
            data = request.POST.copy()
            data["manual-lookup_attempts"] = str(next_attempts)
            manual_name_enabled = False
            lookup_found = False

            if not DOCUMENT_NUMBER_PATTERN.fullmatch(identifier):
                messages.error(
                    request,
                    "Ingresa un numero de documento valido para consultar el tercero.",
                )
            elif schedule_accepts_blacklisted_staff(schedule) and not is_employee_blacklisted(identifier):
                messages.error(
                    request,
                    "Para cargar personal_vario primero debes registrar la cedula en la lista negra.",
                )
            elif is_employee_blacklisted(identifier) and not schedule_accepts_blacklisted_staff(schedule):
                messages.error(
                    request,
                    "Ese numero de documento esta bloqueado en la lista negra y no puede cargarse en horarios.",
                )
            elif ScheduleLine.objects.filter(schedule=schedule, employee_identifier=identifier).exists():
                messages.error(request, "Ese numero de documento ya existe en este horario.")
            else:
                lookup_result = lookup_third_party_by_identifier(identifier)
                if lookup_result:
                    data["manual-employee_name"] = lookup_result.employee_name
                    lookup_found = True
                    messages.success(
                        request,
                        f"Persona encontrada en terceros: {lookup_result.employee_name}. Ahora selecciona el cargo y agrega la persona.",
                    )
                else:
                    manual_name_enabled = next_attempts >= 2
                    if manual_name_enabled:
                        messages.warning(
                            request,
                            "La persona no esta creada en el sistema. Ya puedes ingresar manualmente el nombre y el cargo.",
                        )
                    else:
                        messages.warning(
                            request,
                            "La persona no esta creada en el sistema. Consulta una vez mas para habilitar el cargue manual.",
                        )

            manual_add_form = self.get_manual_add_form(
                schedule,
                data=data,
                readonly=False,
                manual_name_enabled=manual_name_enabled,
                lookup_found=lookup_found,
            )
            return self.render_to_response(
                self.build_context(
                    schedule,
                    manual_add_form=manual_add_form,
                )
            )
        if "manual_add_submit" in request.POST:
            lookup_attempts = int(request.POST.get("manual-lookup_attempts", "0") or "0")
            manual_add_form = self.get_manual_add_form(
                schedule,
                data=request.POST,
                readonly=False,
                manual_name_enabled=lookup_attempts >= 2,
                lookup_found=bool(request.POST.get("manual-employee_name", "").strip()),
            )
            if manual_add_form.is_valid():
                line = manual_add_form.save()
                save_schedule_line_with_balances(line)
                rebuild_balances_for_employees_from_week(schedule.week_start_date, [line.employee_identifier])
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
            touched_employee_ids = sorted(
                {
                    (form.instance.employee_identifier or "").strip()
                    for form in line_formset.forms
                    if form.has_changed() and (form.instance.employee_identifier or "").strip()
                }
            )
            with transaction.atomic():
                updated_schedule = schedule_form.save(commit=False)
                if updated_schedule.status == WeeklySchedule.Status.PUBLISHED:
                    updated_schedule.admin_edit_enabled = False
                updated_schedule.updated_by = request.user
                updated_schedule.save()
                line_formset.save()
                if touched_employee_ids:
                    rebuild_balances_for_employees_from_week(updated_schedule.week_start_date, touched_employee_ids)
                updated_schedule.refresh_from_db()
                if updated_schedule.status == WeeklySchedule.Status.PUBLISHED:
                    generate_and_store_schedule_settlement(
                        updated_schedule,
                        generated_by=request.user,
                        rebuild_balances=False,
                    )

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
        is_admin_scope = user_can_manage_all_sites(self.request.user)
        schedule_form = schedule_form or WeeklyScheduleForm(instance=schedule, readonly=schedule_closed)
        manual_add_form = manual_add_form or self.get_manual_add_form(schedule, readonly=schedule_closed)
        line_formset = line_formset or ScheduleLineFormSet(
            instance=schedule,
            form_kwargs=self.get_line_form_kwargs(schedule, readonly=schedule_closed),
        )
        role_filter_options = sorted(
            {
                (line_form.instance.job_role_name or "").strip()
                for line_form in line_formset.forms
                if (line_form.instance.job_role_name or "").strip()
            },
            key=str.casefold,
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
            "show_night_hours": is_admin_scope,
            "show_detailed_alerts": is_admin_scope,
            "show_admin_balance_controls": is_admin_scope,
            "allow_money_payment": is_admin_scope,
            "manual_add_open": manual_add_form.is_bound,
            "role_filter_options": role_filter_options,
            "schedule_closed": schedule_closed,
            "can_reopen_published": self.can_reopen_published_schedule(schedule),
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


class ScheduleUnlockView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not user_can_manage_all_sites(request.user):
            raise PermissionDenied("Solo administracion puede habilitar edicion sobre horarios publicados.")

        queryset = get_accessible_schedules_queryset(request.user, WeeklySchedule.objects.select_related("site"))
        schedule = get_object_or_404(queryset, pk=self.kwargs["pk"])
        if schedule.status != WeeklySchedule.Status.PUBLISHED:
            messages.info(request, "Solo los horarios publicados pueden habilitarse nuevamente para edicion.")
            return redirect("schedules:edit", pk=schedule.pk)

        schedule.admin_edit_enabled = True
        schedule.updated_by = request.user
        schedule.save(update_fields=["admin_edit_enabled", "updated_by", "updated_at"])
        messages.success(
            request,
            "Horario publicado habilitado temporalmente para edicion. Cuando vuelvas a guardarlo en publicado, se cerrara otra vez.",
        )
        return redirect("schedules:edit", pk=schedule.pk)


class ScheduleDeleteView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not user_can_delete_schedules(request.user):
            raise PermissionDenied("Solo el perfil administrador puede eliminar horarios.")
        queryset = get_accessible_schedules_queryset(request.user, WeeklySchedule.objects.prefetch_related("lines"))
        schedule = get_object_or_404(queryset, pk=self.kwargs["pk"])
        site_name = schedule.site.name
        week_start = schedule.week_start_date
        line_count = schedule.lines.count()
        affected_employee_ids = sorted(
            {
                (line.employee_identifier or "").strip()
                for line in schedule.lines.all()
                if (line.employee_identifier or "").strip()
            }
        )
        with transaction.atomic():
            schedule.delete()
            if affected_employee_ids:
                rebuild_balances_for_employees_from_week(week_start, affected_employee_ids)
        messages.success(
            request,
            f"Horario eliminado: {site_name} - {week_start}. Se borraron {line_count} registros de personal. "
            "Los saldos relacionados se recalcularon automaticamente.",
        )
        return redirect("schedules:list")


class ScheduleExcelDownloadView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        queryset = get_accessible_schedules_queryset(
            request.user,
            WeeklySchedule.objects.select_related("site").prefetch_related("lines"),
        )
        schedule = get_object_or_404(queryset, pk=self.kwargs["pk"])
        return build_schedule_excel_response(schedule)


class InitialBalanceUploadView(LoginRequiredMixin, TemplateView):
    template_name = "schedules/initial_balance_upload.html"

    def dispatch(self, request, *args, **kwargs):
        if not user_can_manage_all_sites(request.user):
            raise PermissionDenied("Solo administracion puede cargar saldos iniciales.")
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, data=None, files=None):
        return InitialBalanceUploadForm(data=data, files=files, prefix="balances")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", self.get_form())
        context["recent_balances"] = EmployeeInitialBalance.objects.order_by("-updated_at", "employee_identifier")[:25]
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form(request.POST, request.FILES)
        if form.is_valid():
            try:
                result = import_employee_initial_balances(
                    form.cleaned_data["file"],
                    updated_by=request.user,
                )
            except ValueError as exc:
                form.add_error("file", str(exc))
            else:
                messages.success(
                    request,
                    "Saldos iniciales cargados correctamente. "
                    f"Creados: {result['created_count']}. Actualizados: {result['updated_count']}.",
                )
                return redirect("schedules:initial-balances")

        messages.error(request, "Revisa el archivo antes de cargar los saldos iniciales.")
        return self.render_to_response(self.get_context_data(form=form))


class InitialBalanceTemplateDownloadView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        if not user_can_manage_all_sites(request.user):
            raise PermissionDenied("Solo administracion puede descargar la plantilla de saldos iniciales.")
        return build_initial_balance_template_response()


class ScheduleSettlementHubView(LoginRequiredMixin, TemplateView):
    template_name = "schedules/settlement_hub.html"

    def get_form(self, data=None):
        return ScheduleSettlementForm(data=data, user=self.request.user, prefix="settlement")

    def get_published_queryset(self):
        queryset = WeeklySchedule.objects.select_related("site").prefetch_related("settlement_document")
        return get_accessible_schedules_queryset(self.request.user, queryset).filter(
            status=WeeklySchedule.Status.PUBLISHED
        ).order_by("-week_start_date", "site__code")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", self.get_form())
        context["recent_published_schedules"] = list(self.get_published_queryset()[:12])
        context["is_admin_scope"] = user_can_manage_all_sites(self.request.user)
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form(request.POST)
        if form.is_valid():
            site = form.cleaned_data.get("site")
            week_start_date = form.cleaned_data["week_start_date"]
            queryset = self.get_published_queryset().filter(week_start_date=week_start_date)
            if site:
                queryset = queryset.filter(site=site)

            schedules = list(queryset[:2])
            if not schedules:
                messages.error(request, "No existe un horario publicado para esa sede y semana.")
            elif len(schedules) > 1:
                messages.error(request, "Selecciona la sede para identificar un unico paz y salvo.")
            else:
                schedule = schedules[0]
                document = generate_and_store_schedule_settlement(schedule, generated_by=request.user)
                response = HttpResponse(document.pdf_content, content_type="application/pdf")
                response["Content-Disposition"] = f'attachment; filename="{document.file_name}"'
                return response

        return self.render_to_response(self.get_context_data(form=form))


class ScheduleSettlementDownloadView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        queryset = WeeklySchedule.objects.select_related("site")
        queryset = get_accessible_schedules_queryset(request.user, queryset)
        schedule = get_object_or_404(queryset, pk=self.kwargs["pk"])
        if schedule.status != WeeklySchedule.Status.PUBLISHED:
            messages.error(request, "El paz y salvo solo esta disponible para horarios publicados.")
            return redirect("schedules:edit", pk=schedule.pk)

        document = generate_and_store_schedule_settlement(schedule, generated_by=request.user)
        response = HttpResponse(document.pdf_content, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{document.file_name}"'
        return response
