from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models

from core.models import Site, SystemConfiguration, TimeStampedModel


class WeeklySchedule(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Borrador"
        REVIEW = "review", "En revision"
        PUBLISHED = "published", "Publicado"

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="weekly_schedules")
    week_start_date = models.DateField()
    week_end_date = models.DateField(blank=True, null=True)
    first_day_index = models.PositiveSmallIntegerField(
        choices=SystemConfiguration.DAY_CHOICES,
        default=SystemConfiguration.SUNDAY,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_schedules",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_schedules",
    )

    class Meta:
        ordering = ["-week_start_date", "site__code"]
        unique_together = ("site", "week_start_date")
        db_table = "horarios_semanales"

    def __str__(self) -> str:
        return f"{self.site.name} - {self.week_start_date:%Y-%m-%d}"

    @property
    def is_closed(self) -> bool:
        return self.status == self.Status.PUBLISHED

    def save(self, *args, **kwargs):
        if isinstance(self.week_start_date, str):
            self.week_start_date = date.fromisoformat(self.week_start_date)
        if self.week_start_date:
            self.week_end_date = self.week_start_date + timedelta(days=6)
        if self.first_day_index is None:
            self.first_day_index = SystemConfiguration.load().week_start_day
        super().save(*args, **kwargs)

    def get_day_columns(self) -> list[dict[str, str]]:
        columns = []
        for index in range(7):
            day_date = self.week_start_date + timedelta(days=index)
            weekday = (self.first_day_index + index) % 7
            columns.append(
                {
                    "index": index,
                    "label": SystemConfiguration.day_name(weekday),
                    "date": day_date.strftime("%d/%m/%Y"),
                    "shift_1_name": f"day_{index}_shift_1",
                    "shift_2_name": f"day_{index}_shift_2",
                    "compensation_mode_name": f"day_{index}_compensation_mode",
                    "compensation_hours_name": f"day_{index}_compensation_hours",
                    "hours_attr": f"day_{index}_hours",
                }
            )
        return columns


class ScheduleLine(TimeStampedModel):
    class CompensationMode(models.TextChoices):
        NONE = "", "Sin pago"
        PAY_DAY = "pay_day", "Pago dia"
        PAY_HOURS = "pay_hours", "Pago horas"

    schedule = models.ForeignKey(WeeklySchedule, on_delete=models.CASCADE, related_name="lines")
    employee_document_type = models.CharField(max_length=20, blank=True)
    employee_identifier = models.CharField(max_length=30)
    employee_name = models.CharField(max_length=180)
    department_code = models.CharField(max_length=20, blank=True)
    department_name = models.CharField(max_length=120, blank=True)
    job_role_code = models.CharField(max_length=30, blank=True)
    job_role_name = models.CharField(max_length=140, blank=True)
    weekly_target_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    daily_max_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))

    day_0_shift_1 = models.CharField(max_length=40, blank=True)
    day_0_shift_2 = models.CharField(max_length=40, blank=True)
    day_0_compensation_mode = models.CharField(max_length=20, choices=CompensationMode.choices, blank=True)
    day_0_compensation_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_0_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_1_shift_1 = models.CharField(max_length=40, blank=True)
    day_1_shift_2 = models.CharField(max_length=40, blank=True)
    day_1_compensation_mode = models.CharField(max_length=20, choices=CompensationMode.choices, blank=True)
    day_1_compensation_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_1_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_2_shift_1 = models.CharField(max_length=40, blank=True)
    day_2_shift_2 = models.CharField(max_length=40, blank=True)
    day_2_compensation_mode = models.CharField(max_length=20, choices=CompensationMode.choices, blank=True)
    day_2_compensation_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_2_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_3_shift_1 = models.CharField(max_length=40, blank=True)
    day_3_shift_2 = models.CharField(max_length=40, blank=True)
    day_3_compensation_mode = models.CharField(max_length=20, choices=CompensationMode.choices, blank=True)
    day_3_compensation_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_3_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_4_shift_1 = models.CharField(max_length=40, blank=True)
    day_4_shift_2 = models.CharField(max_length=40, blank=True)
    day_4_compensation_mode = models.CharField(max_length=20, choices=CompensationMode.choices, blank=True)
    day_4_compensation_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_4_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_5_shift_1 = models.CharField(max_length=40, blank=True)
    day_5_shift_2 = models.CharField(max_length=40, blank=True)
    day_5_compensation_mode = models.CharField(max_length=20, choices=CompensationMode.choices, blank=True)
    day_5_compensation_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_5_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_6_shift_1 = models.CharField(max_length=40, blank=True)
    day_6_shift_2 = models.CharField(max_length=40, blank=True)
    day_6_compensation_mode = models.CharField(max_length=20, choices=CompensationMode.choices, blank=True)
    day_6_compensation_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    day_6_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))

    total_hours = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    overtime_hours = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    night_bonus_hours = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    pending_dates_note = models.TextField(blank=True)
    pending_days = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    pending_hours = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    payment_days_used = models.PositiveSmallIntegerField(default=0)
    payment_hours_used = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    pending_hours_variance = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    validation_summary = models.TextField(blank=True)
    warnings_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["job_role_name", "employee_name", "department_name"]
        unique_together = ("schedule", "employee_identifier")
        db_table = "horarios_detalle"

    def __str__(self) -> str:
        return f"{self.employee_identifier} - {self.employee_name}"

    @property
    def pending_dates(self) -> list[str]:
        return [item.strip() for item in (self.pending_dates_note or "").split(",") if item.strip()]
