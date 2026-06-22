from dataclasses import dataclass
from datetime import date

from django.db import connections

from core.models import Department, JobRole, Site, SystemConfiguration
from legacy.models import LegacyCostCenter, LegacyEmployee, LegacyOperationalSite

SITE_ALIAS_MAP = {
    "001": "RIOJA",
    "002": "JARDIN.N",
    "004": "CANAIMA",
    "005": "CHAPINERO",
    "006": "CENTRO.N",
    "007": "JARDIN.I",
    "008": "SALADO",
    "009": "CENTRO.I",
    "011": "TROPICAL",
    "012": "GRANJAS",
    "013": "CRA5",
    "014": "CANA_BRAVA",
    "016": "UNICO",
}


@dataclass(slots=True)
class OperationalStaffingRecord:
    employee_id: str
    employee_name: str
    site_code: str
    department_code: str
    department_name: str
    role_code: str
    role_name: str


@dataclass(slots=True)
class LegacyThirdPartyRecord:
    employee_id: str
    employee_name: str


def compose_employee_name(
    names: str,
    surname_1: str,
    surname_2: str,
    fallback_name: str = "",
) -> str:
    full_name = " ".join(
        part.strip()
        for part in [names, surname_1, surname_2]
        if part and part.strip()
    ).strip()
    return full_name or fallback_name.strip()


def sync_reference_catalogs() -> dict[str, int]:
    config = SystemConfiguration.load()
    site_count = 0
    department_count = 0
    role_count = 0

    for legacy_site in LegacyOperationalSite.objects.using("legacy").all():
        code = (legacy_site.code or "").strip()
        if not code or code in {"", "XXX"}:
            continue
        site_name = SITE_ALIAS_MAP.get(code, (legacy_site.description or "").strip() or code)
        _, created = Site.objects.update_or_create(
            code=code,
            defaults={
                "name": site_name,
                "is_active": code in SITE_ALIAS_MAP,
            },
        )
        site_count += 1 if created else 0

    active_cost_centers = set(
        LegacyEmployee.objects.using("legacy")
        .filter(contract_status__iexact="A")
        .exclude(cost_center_code__isnull=True)
        .exclude(cost_center_code__exact="")
        .values_list("cost_center_code", flat=True)
    )
    for legacy_department in LegacyCostCenter.objects.using("legacy").filter(code__in=active_cost_centers):
        _, created = Department.objects.update_or_create(
            code=(legacy_department.code or "").strip(),
            defaults={
                "name": (legacy_department.description or "").strip(),
                "is_active": True,
            },
        )
        department_count += 1 if created else 0

    active_roles = (
        LegacyEmployee.objects.using("legacy")
        .filter(contract_status__iexact="A")
        .exclude(role_name__isnull=True)
        .exclude(role_name__exact="")
        .values_list("role_code", "role_name")
        .distinct()
    )
    for role_code, role_name in active_roles:
        _, created = JobRole.objects.update_or_create(
            name=(role_name or "").strip(),
            defaults={
                "code": (role_code or "").strip(),
                "weekly_target_hours": config.default_weekly_hours,
                "daily_max_hours": config.default_daily_max_hours,
                "is_active": True,
            },
        )
        role_count += 1 if created else 0

    return {
        "sites_created": site_count,
        "departments_created": department_count,
        "roles_created": role_count,
    }


def fetch_active_staff_for_site(
    site_code: str,
    week_start_date: date | None = None,
) -> list[OperationalStaffingRecord]:
    cleaned_site_code = (site_code or "").strip()
    if not cleaned_site_code:
        return []

    legacy_query = """
        SELECT DISTINCT
            TRIM(c.id_co) AS id_co_laboral,
            TRIM(c.id_terc) AS id_terc,
            TRIM(COALESCE(t.nombres, '')) AS nombres,
            TRIM(COALESCE(t.apellido1, '')) AS apellido1,
            '' AS apellido2,
            TRIM(COALESCE(c.id_cargo, '')) AS id_cargo,
            TRIM(COALESCE(p.descripcion_cargo, '')) AS descripcion_cargo
        FROM contratos c
        LEFT JOIN terceros t
            ON TRIM(t.codigo) = TRIM(c.id_terc)
        LEFT JOIN (
            SELECT DISTINCT ON (id_contrato)
                id_contrato,
                descripcion_cargo
            FROM nmresumen_pagos_nomina
            ORDER BY id_contrato, lapso_doc DESC, fecha_gen DESC
        ) p
            ON p.id_contrato = c.codigo
        WHERE
            c.estado = 'A'
            AND TRIM(COALESCE(c.grupo_empleados, '')) = '02'
            AND TRIM(c.id_co) = %s
        ORDER BY
            TRIM(c.id_co),
            TRIM(COALESCE(t.apellido1, '')),
            TRIM(COALESCE(t.nombres, ''))
    """
    with connections["legacy"].cursor() as cursor:
        cursor.execute(legacy_query, [cleaned_site_code])
        rows = cursor.fetchall()

    if not rows:
        return []

    employee_ids = [row[1] for row in rows if row[1]]
    employees = list(
        LegacyEmployee.objects.using("legacy")
        .filter(employee_id__in=employee_ids)
        .order_by("cost_center_code", "full_name")
    )
    employee_map = {
        (employee.employee_id or "").strip(): employee
        for employee in employees
    }

    cost_center_map = {
        item.code.strip(): (item.description or "").strip()
        for item in LegacyCostCenter.objects.using("legacy").filter(
            code__in={employee.cost_center_code for employee in employees if employee.cost_center_code}
        )
    }

    staffing_records: list[OperationalStaffingRecord] = []
    seen_employee_ids: set[str] = set()
    for _, employee_id, names, surname_1, surname_2, role_code, role_name in rows:
        normalized_employee_id = (employee_id or "").strip()
        if not normalized_employee_id or normalized_employee_id in seen_employee_ids:
            continue
        seen_employee_ids.add(normalized_employee_id)
        legacy_employee = employee_map.get(normalized_employee_id)
        department_code = (legacy_employee.cost_center_code or "").strip() if legacy_employee else ""
        staffing_records.append(
            OperationalStaffingRecord(
                employee_id=normalized_employee_id,
                employee_name=compose_employee_name(
                    names=names,
                    surname_1=surname_1,
                    surname_2=surname_2,
                    fallback_name=(legacy_employee.full_name or "") if legacy_employee else "",
                ),
                site_code=cleaned_site_code,
                department_code=department_code,
                department_name=cost_center_map.get(department_code, ""),
                role_code=(role_code or "").strip() or ((legacy_employee.role_code or "").strip() if legacy_employee else ""),
                role_name=(role_name or "").strip() or ((legacy_employee.role_name or "").strip() if legacy_employee else ""),
            )
        )

    return sorted(
        staffing_records,
        key=lambda item: (
            (item.role_name or "ZZZ SIN CARGO").casefold(),
            (item.employee_name or "").casefold(),
            (item.employee_id or "").casefold(),
        ),
    )


def lookup_third_party_by_identifier(employee_identifier: str) -> LegacyThirdPartyRecord | None:
    cleaned_identifier = (employee_identifier or "").strip()
    if not cleaned_identifier:
        return None

    lookup_query = """
        SELECT
            TRIM(COALESCE(nombres, '')) AS nombres,
            TRIM(COALESCE(apellido1, '')) AS apellido1,
            TRIM(COALESCE(apellido2, '')) AS apellido2
        FROM terceros
        WHERE TRIM(codigo) = %s
        LIMIT 1
    """
    with connections["legacy"].cursor() as cursor:
        cursor.execute(lookup_query, [cleaned_identifier])
        row = cursor.fetchone()

    if not row:
        return None

    names, surname_1, surname_2 = row
    employee_name = compose_employee_name(names, surname_1, surname_2)
    if not employee_name:
        return None

    return LegacyThirdPartyRecord(
        employee_id=cleaned_identifier,
        employee_name=employee_name,
    )
