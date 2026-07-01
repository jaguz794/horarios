from django.core.management.base import BaseCommand

from schedules.services import (
    build_schedule_balance_audit_rows,
    rebuild_balances_for_employee_from_earliest_schedule,
)


class Command(BaseCommand):
    help = "Audita la coherencia entre saldos guardados en horarios y la reconstruccion por saldo inicial + movimientos."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Recalcula desde la primera semana del trabajador cuando se detecte una diferencia.",
        )

    def handle(self, *args, **options):
        audit_rows = build_schedule_balance_audit_rows()
        mismatches = [row for row in audit_rows if row["has_difference"]]

        if not mismatches:
            self.stdout.write(self.style.SUCCESS("Sin diferencias entre dashboard y auditoria de movimientos."))
            return

        self.stdout.write(
            self.style.WARNING(
                f"Se detectaron {len(mismatches)} trabajador(es) con diferencias de saldo."
            )
        )
        for row in mismatches:
            self.stdout.write(
                (
                    f"{row['site_code']} | {row['site_name']} | {row['job_role_name']} | "
                    f"{row['employee_identifier']} | {row['employee_name']} | "
                    f"dias_auditados={row['audited_day_balance']} | dias_guardados={row['stored_day_balance']} | "
                    f"dif_dias={row['day_difference']} | "
                    f"horas_auditadas={row['audited_hour_balance']} | horas_guardadas={row['stored_hour_balance']} | "
                    f"dif_horas={row['hour_difference']}"
                )
            )

        if not options["fix"]:
            return

        affected_identifiers = [
            str(row["employee_identifier"])
            for row in mismatches
            if str(row["employee_identifier"] or "").strip()
        ]
        fixed_count = 0
        for employee_identifier in affected_identifiers:
            if rebuild_balances_for_employee_from_earliest_schedule(employee_identifier):
                fixed_count += 1

        remaining_rows = build_schedule_balance_audit_rows(affected_identifiers)
        remaining_mismatches = [row for row in remaining_rows if row["has_difference"]]
        if remaining_mismatches:
            self.stdout.write(
                self.style.ERROR(
                    f"Se recalcularon {fixed_count} trabajador(es), pero persisten {len(remaining_mismatches)} diferencia(s)."
                )
            )
            for row in remaining_mismatches:
                self.stdout.write(
                    (
                        f"PENDIENTE | {row['site_code']} | {row['job_role_name']} | "
                        f"{row['employee_identifier']} | dif_dias={row['day_difference']} | "
                        f"dif_horas={row['hour_difference']}"
                    )
                )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Recalculo aplicado correctamente a {fixed_count} trabajador(es) con diferencias."
            )
        )
