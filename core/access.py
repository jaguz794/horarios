from django.db.models import QuerySet

from core.models import Site, UserSiteAccess
from schedules.models import WeeklySchedule


def get_user_site_access(user) -> UserSiteAccess | None:
    if not getattr(user, "is_authenticated", False):
        return None
    access, _ = UserSiteAccess.objects.get_or_create(user=user)
    return access


def user_can_manage_all_sites(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    access = get_user_site_access(user)
    return bool(access and access.can_manage_all_sites)


def user_can_delete_schedules(user) -> bool:
    return user_can_manage_all_sites(user)


def get_accessible_sites_queryset(user, base_queryset: QuerySet | None = None) -> QuerySet:
    queryset = base_queryset if base_queryset is not None else Site.objects.all()
    if user_can_manage_all_sites(user):
        return queryset

    access = get_user_site_access(user)
    if not access:
        return queryset.none()

    return queryset.filter(user_access_profiles=access, admin_only=False).distinct()


def get_accessible_schedules_queryset(user, base_queryset: QuerySet | None = None) -> QuerySet:
    queryset = base_queryset if base_queryset is not None else WeeklySchedule.objects.all()
    if user_can_manage_all_sites(user):
        return queryset

    site_queryset = get_accessible_sites_queryset(user)
    return queryset.filter(site__in=site_queryset).distinct()
