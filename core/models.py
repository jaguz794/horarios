from django.conf import settings
from datetime import time
from decimal import Decimal

from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Site(TimeStampedModel):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=120)
    legacy_name = models.CharField(max_length=120, blank=True)
    group_code = models.CharField(max_length=10, blank=True)
    city = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        db_table = "sedes"
        verbose_name = "Sede"
        verbose_name_plural = "Sedes"

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class UserSiteAccess(TimeStampedModel):
    class Role(models.TextChoices):
        ADMIN = "admin", "Administrador"
        SITE_USER = "site_user", "Usuario por sede"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="site_access",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.SITE_USER)
    sites = models.ManyToManyField(
        Site,
        blank=True,
        related_name="user_access_profiles",
        db_table="accesos_usuario_sede_sedes",
    )

    class Meta:
        verbose_name = "Acceso por usuario"
        verbose_name_plural = "Accesos por usuario"
        ordering = ["user__username"]
        db_table = "accesos_usuario_sede"

    def __str__(self) -> str:
        return f"{self.user.username} - {self.get_role_display()}"

    @property
    def can_manage_all_sites(self) -> bool:
        return self.user.is_superuser or self.role == self.Role.ADMIN


class Department(TimeStampedModel):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        db_table = "areas"
        verbose_name = "Departamento"
        verbose_name_plural = "Departamentos"

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class JobRole(TimeStampedModel):
    code = models.CharField(max_length=30, blank=True)
    name = models.CharField(max_length=140, unique=True)
    weekly_target_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("46.00"),
    )
    daily_max_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("9.00"),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        db_table = "parametros_cargos"
        verbose_name = "Cargo"
        verbose_name_plural = "Cargos"

    def __str__(self) -> str:
        return self.name


class ShiftTemplate(TimeStampedModel):
    code = models.CharField(max_length=30, blank=True)
    label = models.CharField(max_length=40, unique=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    duration_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    night_bonus_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    display_order = models.PositiveIntegerField(default=0)
    counts_as_worked_time = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["display_order", "label"]
        db_table = "catalogo_turnos"
        verbose_name = "Turno"
        verbose_name_plural = "Turnos"

    def __str__(self) -> str:
        return self.label

    @property
    def spans_next_day(self) -> bool:
        return bool(self.start_time and self.end_time and self.end_time <= self.start_time)

    @property
    def is_full_day_shift(self) -> bool:
        return self.counts_as_worked_time and self.duration_hours >= Decimal("7.50")

    @property
    def starts_in_morning(self) -> bool:
        return bool(self.start_time and self.start_time < time(12, 0))

    @property
    def starts_in_afternoon(self) -> bool:
        return bool(self.start_time and self.start_time >= time(12, 0))

    @property
    def can_be_second_shift(self) -> bool:
        return (
            self.counts_as_worked_time
            and self.start_time is not None
            and (self.start_time >= time(12, 0) or self.spans_next_day)
            and not self.is_full_day_shift
        )

    @property
    def choice_group(self) -> str:
        if not self.counts_as_worked_time:
            return "Novedades"
        if self.spans_next_day or (self.start_time and self.start_time >= time(18, 0)):
            return "Turnos nocturnos"
        if self.is_full_day_shift:
            return "Jornada continua"
        if self.start_time and self.start_time < time(12, 0):
            return "Primer turno"
        return "Segundo turno"


class SystemConfiguration(TimeStampedModel):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6
    DAY_CHOICES = [
        (MONDAY, "Lunes"),
        (TUESDAY, "Martes"),
        (WEDNESDAY, "Miercoles"),
        (THURSDAY, "Jueves"),
        (FRIDAY, "Viernes"),
        (SATURDAY, "Sabado"),
        (SUNDAY, "Domingo"),
    ]

    organization_name = models.CharField(max_length=120, default="Portal de Horarios")
    week_start_day = models.PositiveSmallIntegerField(choices=DAY_CHOICES, default=SUNDAY)
    default_weekly_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("46.00"),
    )
    default_daily_max_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("9.00"),
    )
    night_shift_start = models.TimeField(default=time(19, 0))

    class Meta:
        verbose_name = "Configuracion del sistema"
        verbose_name_plural = "Configuracion del sistema"
        db_table = "configuracion_sistema"

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SystemConfiguration":
        config, _ = cls.objects.get_or_create(pk=1)
        return config

    @classmethod
    def day_name(cls, day_index: int) -> str:
        return dict(cls.DAY_CHOICES).get(day_index, "Dia")

    def __str__(self) -> str:
        return self.organization_name
