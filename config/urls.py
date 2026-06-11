from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Portal de Horarios"
admin.site.site_title = "Portal de Horarios"
admin.site.index_title = "Administracion"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("cuentas/", include("django.contrib.auth.urls")),
    path("horarios/", include(("schedules.urls", "schedules"), namespace="schedules")),
    path("", include("core.urls")),
]

