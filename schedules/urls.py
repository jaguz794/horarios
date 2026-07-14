from django.urls import path

from schedules.views import (
    InitialBalanceTemplateDownloadView,
    InitialBalanceUploadView,
    ScheduleDeleteView,
    ScheduleEditView,
    ScheduleExcelDownloadView,
    ScheduleFlatFileTemplateDownloadView,
    ScheduleFlatFileUploadView,
    ScheduleListView,
    ScheduleLoadView,
    ScheduleRefreshView,
    ScheduleSettlementDownloadView,
    ScheduleSettlementHubView,
    ScheduleUnlockView,
)

urlpatterns = [
    path("", ScheduleListView.as_view(), name="list"),
    path("nuevo/", ScheduleLoadView.as_view(), name="load"),
    path("saldos-iniciales/", InitialBalanceUploadView.as_view(), name="initial-balances"),
    path("saldos-iniciales/plantilla/", InitialBalanceTemplateDownloadView.as_view(), name="initial-balances-template"),
    path("paz-y-salvo/", ScheduleSettlementHubView.as_view(), name="settlement-hub"),
    path("<int:pk>/paz-y-salvo/", ScheduleSettlementDownloadView.as_view(), name="settlement-download"),
    path("<int:pk>/excel/", ScheduleExcelDownloadView.as_view(), name="excel-download"),
    path("<int:pk>/archivo-plano/plantilla/", ScheduleFlatFileTemplateDownloadView.as_view(), name="flatfile-template"),
    path("<int:pk>/archivo-plano/cargar/", ScheduleFlatFileUploadView.as_view(), name="flatfile-upload"),
    path("<int:pk>/editar/", ScheduleEditView.as_view(), name="edit"),
    path("<int:pk>/eliminar/", ScheduleDeleteView.as_view(), name="delete"),
    path("<int:pk>/recargar-personal/", ScheduleRefreshView.as_view(), name="refresh"),
    path("<int:pk>/habilitar-edicion/", ScheduleUnlockView.as_view(), name="unlock"),
]
