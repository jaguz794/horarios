from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from core.models import Department, JobRole, ShiftTemplate, Site, SystemConfiguration, UserSiteAccess

User = get_user_model()


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    list_display = ("organization_name", "week_start_day", "default_weekly_hours", "default_daily_max_hours")

    def has_add_permission(self, request):
        if SystemConfiguration.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("code",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(JobRole)
class JobRoleAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "weekly_target_hours", "daily_max_hours", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")


@admin.register(ShiftTemplate)
class ShiftTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "code",
        "duration_hours",
        "night_bonus_hours",
        "counts_as_worked_time",
        "is_active",
    )
    list_filter = ("counts_as_worked_time", "is_active")
    search_fields = ("label", "code")


class OrderedSitesChoicesMixin:
    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "sites":
            kwargs["queryset"] = Site.objects.order_by("code")
        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(UserSiteAccess)
class UserSiteAccessAdmin(OrderedSitesChoicesMixin, admin.ModelAdmin):
    list_display = ("user", "role", "allowed_sites")
    list_filter = ("role", "sites")
    search_fields = ("user__username", "user__first_name", "user__last_name", "user__email")
    filter_horizontal = ("sites",)
    autocomplete_fields = ("user",)

    def allowed_sites(self, obj):
        if obj.can_manage_all_sites:
            return "Todas"
        site_names = list(obj.sites.order_by("code").values_list("name", flat=True)[:5])
        if not site_names:
            return "Sin sedes"
        suffix = "" if obj.sites.count() <= 5 else "..."
        return ", ".join(site_names) + suffix
    allowed_sites.short_description = "Sedes asignadas"


class UserSiteAccessInline(OrderedSitesChoicesMixin, admin.StackedInline):
    model = UserSiteAccess
    fk_name = "user"
    can_delete = False
    extra = 0
    max_num = 1
    filter_horizontal = ("sites",)
    verbose_name = "Acceso al portal"
    verbose_name_plural = "Acceso al portal"
    fields = ("role", "sites")

    def get_extra(self, request, obj=None, **kwargs):
        if obj is None:
            return 0
        try:
            obj.site_access
        except UserSiteAccess.DoesNotExist:
            return 1
        return 0

    def has_delete_permission(self, request, obj=None):
        return False


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    inlines = (UserSiteAccessInline,)
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "portal_role",
        "portal_sites",
    )

    def get_inline_instances(self, request, obj=None):
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("site_access").prefetch_related("site_access__sites")

    def _get_site_access(self, obj):
        try:
            return obj.site_access
        except UserSiteAccess.DoesNotExist:
            return None

    def portal_role(self, obj):
        if obj.is_superuser:
            return "Superusuario"
        access = self._get_site_access(obj)
        if not access:
            return "Sin perfil"
        return access.get_role_display()

    portal_role.short_description = "Perfil portal"

    def portal_sites(self, obj):
        if obj.is_superuser:
            return "Todas"
        access = self._get_site_access(obj)
        if not access:
            return "Sin perfil"
        if access.can_manage_all_sites:
            return "Todas"
        sites = list(access.sites.order_by("code").values_list("code", flat=True))
        return ", ".join(sites) if sites else "Sin sedes"

    portal_sites.short_description = "Sedes portal"
