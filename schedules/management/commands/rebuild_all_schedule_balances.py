from django.core.management.base import BaseCommand

from schedules.models import ScheduleLine
from schedules.services import rebuild_balances_for_employee_from_earliest_schedule


class Command(BaseCommand):
    help = "Recalcula todos los horarios y saldos por empleado desde su primera semana registrada."

    def add_arguments(self, parser):
        parser.add_argument(
            "--employee",
            action="append",
            dest="employees",
            default=[],
            help="Cedula especifica a recalcular. Puede repetirse varias veces.",
        )

    def handle(self, *args, **options):
        requested_employees = [
            (employee_identifier or "").strip()
            for employee_identifier in options.get("employees") or []
            if (employee_identifier or "").strip()
        ]

        if requested_employees:
            employee_identifiers = requested_employees
        else:
            employee_identifiers = list(
                ScheduleLine.objects.exclude(employee_identifier="")
                .order_by()
                .values_list("employee_identifier", flat=True)
                .distinct()
            )

        total = len(employee_identifiers)
        rebuilt = 0

        self.stdout.write(f"Empleados a recalcular: {total}")
        for position, employee_identifier in enumerate(employee_identifiers, start=1):
            rebuilt_flag = rebuild_balances_for_employee_from_earliest_schedule(employee_identifier)
            if rebuilt_flag:
                rebuilt += 1
                self.stdout.write(
                    self.style.SUCCESS(f"[{position}/{total}] {employee_identifier}: recalculado")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"[{position}/{total}] {employee_identifier}: sin horarios para recalcular")
                )

        self.stdout.write(self.style.SUCCESS(f"Proceso completado. Recalculados: {rebuilt}/{total}"))
