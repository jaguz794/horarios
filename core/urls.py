from django.urls import path

from core.views import DashboardView, SiteListView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("sedes/", SiteListView.as_view(), name="site-list"),
]
