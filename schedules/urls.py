from django.urls import path

from schedules.views import (
    ScheduleDeleteView,
    ScheduleEditView,
    ScheduleListView,
    ScheduleLoadView,
    ScheduleRefreshView,
    ScheduleSettlementDownloadView,
    ScheduleSettlementHubView,
)

urlpatterns = [
    path("", ScheduleListView.as_view(), name="list"),
    path("nuevo/", ScheduleLoadView.as_view(), name="load"),
    path("paz-y-salvo/", ScheduleSettlementHubView.as_view(), name="settlement-hub"),
    path("<int:pk>/paz-y-salvo/", ScheduleSettlementDownloadView.as_view(), name="settlement-download"),
    path("<int:pk>/editar/", ScheduleEditView.as_view(), name="edit"),
    path("<int:pk>/eliminar/", ScheduleDeleteView.as_view(), name="delete"),
    path("<int:pk>/recargar-personal/", ScheduleRefreshView.as_view(), name="refresh"),
]
