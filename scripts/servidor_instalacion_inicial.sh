#!/bin/sh
set -eu

TURNOS_ARCHIVO="${1:-}"

cd /opt/horarios/app

docker compose up -d --build
docker compose exec web python manage.py sync_legacy_catalogs

if [ -n "$TURNOS_ARCHIVO" ]; then
  if [ ! -f "$TURNOS_ARCHIVO" ]; then
    echo "No existe el archivo de turnos: $TURNOS_ARCHIVO" >&2
    exit 1
  fi

  CONTENEDOR_ID="$(docker compose ps -q web)"
  docker cp "$TURNOS_ARCHIVO" "${CONTENEDOR_ID}:/tmp/turnos.xlsx"
  docker compose exec web python manage.py import_shift_templates --file /tmp/turnos.xlsx
fi

echo "Instalacion inicial completada."
echo "Si aun no existe el usuario administrador, crea uno con:"
echo "docker compose exec web python manage.py createsuperuser"
