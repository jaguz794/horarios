from unittest.mock import patch

from django.test import TestCase

from core.models import OperationalStaffCache
from legacy.services import OperationalStaffingRecord, fetch_active_staff_for_site, sync_operational_staff_cache


class OperationalStaffCacheTests(TestCase):
    def test_fetch_active_staff_for_site_uses_local_cache(self):
        OperationalStaffCache.objects.create(
            site_code="007",
            employee_identifier="123456",
            employee_name="Empleado Cache",
            department_code="A1",
            department_name="ABASTOS",
            role_code="AUX",
            role_name="AUXILIAR",
        )

        with patch("legacy.services.fetch_operational_staff_from_legacy", side_effect=AssertionError("No debe consultar legacy")):
            records = fetch_active_staff_for_site("007")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].employee_id, "123456")
        self.assertEqual(records[0].employee_name, "Empleado Cache")

    @patch("legacy.services.fetch_operational_staff_from_legacy")
    def test_sync_operational_staff_cache_replaces_target_site_only(self, mock_fetch):
        OperationalStaffCache.objects.create(
            site_code="007",
            employee_identifier="OLD007",
            employee_name="Viejo 007",
            role_name="AUXILIAR",
        )
        OperationalStaffCache.objects.create(
            site_code="008",
            employee_identifier="KEEP008",
            employee_name="Se Conserva",
            role_name="CAJERO",
        )
        mock_fetch.return_value = [
            OperationalStaffingRecord(
                employee_id="NEW007",
                employee_name="Nuevo 007",
                site_code="007",
                department_code="C1",
                department_name="CARNES",
                role_code="CARN",
                role_name="CARNICERO",
            )
        ]

        result = sync_operational_staff_cache(site_codes=["007"])

        self.assertEqual(result["site_count"], 1)
        self.assertEqual(result["record_count"], 1)
        self.assertFalse(OperationalStaffCache.objects.filter(site_code="007", employee_identifier="OLD007").exists())
        self.assertTrue(OperationalStaffCache.objects.filter(site_code="007", employee_identifier="NEW007").exists())
        self.assertTrue(OperationalStaffCache.objects.filter(site_code="008", employee_identifier="KEEP008").exists())
