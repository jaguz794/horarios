from django.core.management.base import BaseCommand
from django.db import connections


RECOMMENDED_INDEXES = [
    {
        "name": "idx_contratos_horarios_operativos",
        "table": "contratos",
        "description": "Acelera los filtros por sede, grupo operativo y estado.",
        "create_sql": """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_contratos_horarios_operativos
            ON contratos ((TRIM(id_co)), (TRIM(COALESCE(grupo_empleados, ''))), estado)
        """.strip(),
    },
    {
        "name": "idx_terceros_codigo_trim",
        "table": "terceros",
        "description": "Acelera la busqueda del tercero por codigo normalizado.",
        "create_sql": """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_terceros_codigo_trim
            ON terceros ((TRIM(codigo)))
        """.strip(),
    },
    {
        "name": "idx_nmresumen_nomina_contrato_fecha",
        "table": "nmresumen_pagos_nomina",
        "description": "Reduce el costo del DISTINCT ON usado para hallar el ultimo cargo por contrato.",
        "create_sql": """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_nmresumen_nomina_contrato_fecha
            ON nmresumen_pagos_nomina (id_contrato, lapso_doc DESC, fecha_gen DESC)
        """.strip(),
    },
]


class Command(BaseCommand):
    help = "Revisa y opcionalmente crea los indices recomendados en la base legacy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Crea los indices faltantes en la base legacy con CREATE INDEX CONCURRENTLY.",
        )

    def handle(self, *args, **options):
        connection = connections["legacy"]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT indexname, tablename, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                """
            )
            existing_indexes = {
                row[0]: {
                    "table": row[1],
                    "definition": row[2],
                }
                for row in cursor.fetchall()
            }

        missing_indexes: list[dict[str, str]] = []
        for index in RECOMMENDED_INDEXES:
            if index["name"] in existing_indexes:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[OK] {index['name']} en {index['table']}: {index['description']}"
                    )
                )
            else:
                missing_indexes.append(index)
                self.stdout.write(
                    self.style.WARNING(
                        f"[FALTA] {index['name']} en {index['table']}: {index['description']}"
                    )
                )

        if not options["apply"]:
            if missing_indexes:
                self.stdout.write(
                    "Ejecuta este mismo comando con --apply para crear los indices faltantes."
                )
            return

        if not missing_indexes:
            self.stdout.write(self.style.SUCCESS("No hay indices faltantes por crear."))
            return

        autocommit_state = connection.get_autocommit()
        connection.set_autocommit(True)
        try:
            with connection.cursor() as cursor:
                for index in missing_indexes:
                    self.stdout.write(f"Creando {index['name']}...")
                    cursor.execute(index["create_sql"])
        finally:
            connection.set_autocommit(autocommit_state)

        self.stdout.write(self.style.SUCCESS("Indices legacy creados correctamente."))
