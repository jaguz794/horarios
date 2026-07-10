from dataclasses import dataclass
from datetime import date
import logging
import re

from django.db import connections, transaction
from django.utils import timezone

from core.models import Department, JobRole, OperationalStaffCache, Site, SystemConfiguration
from legacy.models import LegacyCostCenter, LegacyEmployee, LegacyOperationalSite

logger = logging.getLogger(__name__)
DOCUMENT_NUMBER_PATTERN = re.compile(r"^[0-9A-Za-z-]{3,30}$")

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
    site_code: str = ""
    role_code: str = ""
    role_name: str = ""


class LegacyStaffLookupError(RuntimeError):
    pass


class LegacyStaffAmbiguousMatchError(LegacyStaffLookupError):
    pass


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


def _build_operational_staff_records(
    rows: list[tuple[str, str, str, str, str, str, str]],
    *,
    site_filter: set[str] | None = None,
) -> list[OperationalStaffingRecord]:
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
    seen_employee_site_pairs: set[tuple[str, str]] = set()
    for site_code, employee_id, names, surname_1, surname_2, role_code, role_name in rows:
        normalized_site_code = (site_code or "").strip()
        normalized_employee_id = (employee_id or "").strip()
        if not normalized_site_code or not normalized_employee_id:
            continue
        if site_filter is not None and normalized_site_code not in site_filter:
            continue
        pair = (normalized_site_code, normalized_employee_id)
        if pair in seen_employee_site_pairs:
            continue
        seen_employee_site_pairs.add(pair)
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
                site_code=normalized_site_code,
                department_code=department_code,
                department_name=cost_center_map.get(department_code, ""),
                role_code=(role_code or "").strip() or ((legacy_employee.role_code or "").strip() if legacy_employee else ""),
                role_name=(role_name or "").strip() or ((legacy_employee.role_name or "").strip() if legacy_employee else ""),
            )
        )

    return sorted(
        staffing_records,
        key=lambda item: (
            (item.site_code or "").casefold(),
            (item.role_name or "ZZZ SIN CARGO").casefold(),
            (item.employee_name or "").casefold(),
            (item.employee_id or "").casefold(),
        ),
    )


def fetch_operational_staff_from_legacy(
    site_codes: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[OperationalStaffingRecord]:
    cleaned_site_codes = sorted(
        {
            (site_code or "").strip()
            for site_code in (site_codes or [])
            if (site_code or "").strip()
        }
    )

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
    """
    params: list[str] = []
    if cleaned_site_codes:
        placeholders = ", ".join(["%s"] * len(cleaned_site_codes))
        legacy_query += f"\n            AND TRIM(c.id_co) IN ({placeholders})\n"
        params.extend(cleaned_site_codes)
    legacy_query += """
        ORDER BY
            TRIM(c.id_co),
            TRIM(COALESCE(t.apellido1, '')),
            TRIM(COALESCE(t.nombres, ''))
    """

    try:
        with connections["legacy"].cursor() as cursor:
            cursor.execute(legacy_query, params)
            rows = cursor.fetchall()
    except Exception as exc:
        logger.exception(
            "Fallo consultando personal operativo legacy para sedes=%s",
            cleaned_site_codes or "todas",
        )
        raise LegacyStaffLookupError(
            "No fue posible consultar el personal desde la base de datos."
        ) from exc

    site_filter = set(cleaned_site_codes) if cleaned_site_codes else None
    return _build_operational_staff_records(rows, site_filter=site_filter)


def _build_cached_operational_staff_records(site_code: str) -> list[OperationalStaffingRecord]:
    cached_rows = list(
        OperationalStaffCache.objects.filter(
            site_code=site_code,
            is_active=True,
        ).order_by("role_name", "employee_name", "employee_identifier")
    )
    return [
        OperationalStaffingRecord(
            employee_id=row.employee_identifier,
            employee_name=row.employee_name,
            site_code=row.site_code,
            department_code=row.department_code,
            department_name=row.department_name,
            role_code=row.role_code,
            role_name=row.role_name,
        )
        for row in cached_rows
    ]


def replace_operational_staff_cache(
    records: list[OperationalStaffingRecord],
    *,
    target_site_codes: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, int]:
    cleaned_target_site_codes = sorted(
        {
            (site_code or "").strip()
            for site_code in (target_site_codes or [])
            if (site_code or "").strip()
        }
    )
    sync_timestamp = timezone.now()
    created_rows = [
        OperationalStaffCache(
            site_code=record.site_code,
            employee_identifier=record.employee_id,
            employee_name=record.employee_name,
            department_code=record.department_code,
            department_name=record.department_name,
            role_code=record.role_code,
            role_name=record.role_name,
            is_active=True,
            created_at=sync_timestamp,
            updated_at=sync_timestamp,
        )
        for record in records
    ]

    deleted_count = 0
    with transaction.atomic():
        if cleaned_target_site_codes:
            deleted_count = OperationalStaffCache.objects.filter(site_code__in=cleaned_target_site_codes).delete()[0]
        else:
            deleted_count = OperationalStaffCache.objects.all().delete()[0]
        if created_rows:
            OperationalStaffCache.objects.bulk_create(created_rows, batch_size=1000)

    effective_site_codes = cleaned_target_site_codes or sorted({record.site_code for record in records})
    return {
        "site_count": len(effective_site_codes),
        "record_count": len(created_rows),
        "deleted_count": deleted_count,
    }


def sync_operational_staff_cache(
    site_codes: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, int]:
    cleaned_site_codes = sorted(
        {
            (site_code or "").strip()
            for site_code in (site_codes or [])
            if (site_code or "").strip()
        }
    )
    records = fetch_operational_staff_from_legacy(cleaned_site_codes or None)
    return replace_operational_staff_cache(records, target_site_codes=cleaned_site_codes or None)


def fetch_active_staff_for_site(
    site_code: str,
    week_start_date: date | None = None,
) -> list[OperationalStaffingRecord]:
    cleaned_site_code = (site_code or "").strip()
    if not cleaned_site_code:
        return []

    cached_records = _build_cached_operational_staff_records(cleaned_site_code)
    if cached_records:
        return cached_records

    legacy_records = [
        record
        for record in fetch_operational_staff_from_legacy([cleaned_site_code])
        if record.site_code == cleaned_site_code
    ]
    if legacy_records:
        replace_operational_staff_cache(legacy_records, target_site_codes=[cleaned_site_code])
        return _build_cached_operational_staff_records(cleaned_site_code)

    return sorted(
        legacy_records,
        key=lambda item: (
            (item.role_name or "ZZZ SIN CARGO").casefold(),
            (item.employee_name or "").casefold(),
            (item.employee_id or "").casefold(),
        ),
    )


def lookup_third_party_by_identifier(employee_identifier: str) -> LegacyThirdPartyRecord | None:
    cleaned_query = " ".join((employee_identifier or "").split()).strip()
    if not cleaned_query:
        return None
    identifier_query = """
        SELECT DISTINCT
            TRIM(c.id_terc) AS id_terc,
            TRIM(COALESCE(t.nombres, '')) AS nombres,
            TRIM(COALESCE(t.apellido1, '')) AS apellido1,
            TRIM(COALESCE(t.apellido2, '')) AS apellido2,
            TRIM(COALESCE(c.id_co, '')) AS id_co,
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
            AND TRIM(c.id_terc) = %s
        ORDER BY
            TRIM(COALESCE(t.apellido1, '')),
            TRIM(COALESCE(t.nombres, ''))
        LIMIT 1
    """
    third_party_query = """
        SELECT
            TRIM(COALESCE(codigo, '')) AS codigo,
            TRIM(COALESCE(nombres, '')) AS nombres,
            TRIM(COALESCE(apellido1, '')) AS apellido1,
            TRIM(COALESCE(apellido2, '')) AS apellido2
        FROM terceros
        WHERE TRIM(codigo) = %s
        LIMIT 1
    """
    name_lookup_query = """
        SELECT DISTINCT
            TRIM(c.id_terc) AS id_terc,
            TRIM(COALESCE(t.nombres, '')) AS nombres,
            TRIM(COALESCE(t.apellido1, '')) AS apellido1,
            TRIM(COALESCE(t.apellido2, '')) AS apellido2,
            TRIM(COALESCE(c.id_co, '')) AS id_co,
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
            AND UPPER(
                TRIM(
                    COALESCE(t.nombres, '') || ' ' || COALESCE(t.apellido1, '') || ' ' || COALESCE(t.apellido2, '')
                )
            ) LIKE UPPER(%s)
        ORDER BY
            TRIM(COALESCE(t.apellido1, '')),
            TRIM(COALESCE(t.nombres, ''))
        LIMIT 10
    """
    try:
        with connections["legacy"].cursor() as cursor:
            if DOCUMENT_NUMBER_PATTERN.fullmatch(cleaned_query):
                cursor.execute(identifier_query, [cleaned_query.upper()])
                row = cursor.fetchone()
                if row:
                    employee_id, names, surname_1, surname_2, site_code, role_code, role_name = row
                    employee_name = compose_employee_name(names, surname_1, surname_2)
                    return LegacyThirdPartyRecord(
                        employee_id=(employee_id or cleaned_query).strip(),
                        employee_name=employee_name,
                        site_code=(site_code or "").strip(),
                        role_code=(role_code or "").strip(),
                        role_name=(role_name or "").strip(),
                    )

                cursor.execute(third_party_query, [cleaned_query.upper()])
                third_party_row = cursor.fetchone()
                if not third_party_row:
                    return None
                employee_id, names, surname_1, surname_2 = third_party_row
                employee_name = compose_employee_name(names, surname_1, surname_2)
                if not employee_name:
                    return None
                return LegacyThirdPartyRecord(
                    employee_id=(employee_id or cleaned_query).strip(),
                    employee_name=employee_name,
                )

            like_query = f"%{'%'.join(cleaned_query.split())}%"
            cursor.execute(name_lookup_query, [like_query])
            rows = cursor.fetchall()
    except Exception as exc:
        logger.exception("Fallo consultando terceros legacy para termino=%s", cleaned_query)
        raise LegacyStaffLookupError(
            "No fue posible consultar el personal desde la base de datos."
        ) from exc

    if not rows:
        return None
    if len(rows) > 1:
        raise LegacyStaffAmbiguousMatchError(
            "Se encontraron varias personas con ese nombre. Usa la cedula para continuar."
        )

    employee_id, names, surname_1, surname_2, site_code, role_code, role_name = rows[0]
    employee_name = compose_employee_name(names, surname_1, surname_2)
    if not employee_name:
        return None

    return LegacyThirdPartyRecord(
        employee_id=(employee_id or "").strip(),
        employee_name=employee_name,
        site_code=(site_code or "").strip(),
        role_code=(role_code or "").strip(),
        role_name=(role_name or "").strip(),
    )
