#!/bin/sh
set -eu

BRANCH="${1:-master}"

cd /opt/horarios/app
git fetch --all
git checkout "$BRANCH"
git pull origin "$BRANCH"
docker compose up -d --build

echo "Actualizacion completada en la rama $BRANCH."
