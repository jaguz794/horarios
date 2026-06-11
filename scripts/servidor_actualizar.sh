#!/bin/sh
set -eu

BRANCH="${1:-master}"

echo "Iniciando actualizacion del portal..."
echo "Rama objetivo: $BRANCH"

cd /opt/horarios/app
echo "Ruta actual: $(pwd)"
echo "Actualizando repositorio..."
git fetch --all
git checkout "$BRANCH"
git pull origin "$BRANCH"
echo "Reconstruyendo contenedores..."
docker compose up -d --build

echo "Actualizacion completada en la rama $BRANCH."
