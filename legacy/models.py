from django.db import models


class LegacyOperationalSite(models.Model):
    code = models.CharField(max_length=10, primary_key=True, db_column="codigo")
    description = models.CharField(max_length=120, blank=True, db_column="descripcion")
    group_code = models.CharField(max_length=10, blank=True, db_column="grupo_co")

    class Meta:
        managed = False
        db_table = "centro_operacion"
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.description}"


class LegacyCostCenter(models.Model):
    code = models.CharField(max_length=20, primary_key=True, db_column="codigo")
    description = models.CharField(max_length=120, blank=True, db_column="descripcion")

    class Meta:
        managed = False
        db_table = "centro_costo"
        ordering = ["description"]

    def __str__(self) -> str:
        return f"{self.code} - {self.description}"


class LegacyEmployee(models.Model):
    employee_id = models.CharField(max_length=30, primary_key=True, db_column="empleado")
    full_name = models.CharField(max_length=180, blank=True, db_column="nombre_completo")
    site_code = models.CharField(max_length=10, blank=True, db_column="id_co")
    cost_center_code = models.CharField(max_length=20, blank=True, db_column="id_ccosto")
    role_code = models.CharField(max_length=20, blank=True, db_column="id_cargo")
    role_name = models.CharField(max_length=180, blank=True, db_column="nombre_cargo")
    contract_status = models.CharField(max_length=5, blank=True, db_column="estado_contrato")
    withdrawal_date = models.CharField(max_length=20, blank=True, db_column="fecha_retiro")

    class Meta:
        managed = False
        db_table = "hojas_de_vida"
        ordering = ["full_name"]

    def __str__(self) -> str:
        return f"{self.employee_id} - {self.full_name}"

