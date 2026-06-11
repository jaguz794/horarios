#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-/opt/horarios/app}"
BRANCH="${BRANCH:-master}"

cd "$APP_DIR"
git fetch --all
git checkout "$BRANCH"
git pull origin "$BRANCH"
docker compose up -d --build
