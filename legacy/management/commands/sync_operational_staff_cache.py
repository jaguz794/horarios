from django.core.management.base import BaseCommand

from legacy.services import sync_operational_staff_cache


class Command(BaseCommand):
    help = "Sincroniza el personal operativo desde biable01 hacia la cache local del portal."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site",
            dest="sites",
            action="append",
            help="Codigo de sede a sincronizar. Puedes repetir --site varias veces.",
        )

    def handle(self, *args, **options):
        result = sync_operational_staff_cache(site_codes=options.get("sites"))
        self.stdout.write(
            self.style.SUCCESS(
                "Cache sincronizada. "
                f"Sedes: {result['site_count']}. "
                f"Registros cargados: {result['record_count']}. "
                f"Registros reemplazados: {result['deleted_count']}."
            )
        )
