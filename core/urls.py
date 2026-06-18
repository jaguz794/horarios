from django.urls import path

from core.views import DashboardView, ReportHubView, SiteListView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("sedes/", SiteListView.as_view(), name="site-list"),
    path("informes/", ReportHubView.as_view(), name="reports"),
]
