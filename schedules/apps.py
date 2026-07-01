from django.apps import AppConfig


class SchedulesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schedules"
    verbose_name = "Horarios"

    def ready(self):
        import schedules.signals  # noqa: F401
