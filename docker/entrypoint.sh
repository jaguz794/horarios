#!/bin/sh
set -eu

if [ -n "${DB_NAME:-}" ]; then
  echo "Esperando la base de datos principal..."
  python - <<'PY'
import os
import time
import psycopg

host = os.getenv("DB_HOST", "")
port = int(os.getenv("DB_PORT", "5432"))
dbname = os.getenv("DB_NAME", "")
user = os.getenv("DB_USER", "")
password = os.getenv("DB_PASSWORD", "")

for attempt in range(1, 31):
    try:
        with psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            connect_timeout=5,
        ):
            print("Base de datos disponible.")
            break
    except Exception as exc:
        if attempt == 30:
            raise
        print(f"Intento {attempt}/30 sin conexion a PostgreSQL: {exc}")
        time.sleep(2)
PY
fi

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ "${SYNC_CATALOGS_ON_START:-0}" = "1" ]; then
  python manage.py sync_legacy_catalogs
fi

if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && \
   [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ] && \
   [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ]; then
  python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ["DJANGO_SUPERUSER_USERNAME"]
email = os.environ["DJANGO_SUPERUSER_EMAIL"]
password = os.environ["DJANGO_SUPERUSER_PASSWORD"]

if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username=username, email=email, password=password)
    print("Superusuario creado automaticamente.")
else:
    print("El superusuario ya existe, no se crea nuevamente.")
PY
fi

exec "$@"
