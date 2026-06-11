from django.core.management.base import BaseCommand

from legacy.services import sync_reference_catalogs


class Command(BaseCommand):
    help = "Sincroniza sedes, areas y cargos desde la base legacy."

    def handle(self, *args, **options):
        result = sync_reference_catalogs()
        self.stdout.write(
            self.style.SUCCESS(
                "Sincronizacion completada. "
                f"Sedes nuevas: {result['sites_created']}. "
                f"Areas nuevas: {result['departments_created']}. "
                f"Cargos nuevos: {result['roles_created']}."
            )
        )

