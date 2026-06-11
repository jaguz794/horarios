# Portal de Horarios

Base inicial para migrar el archivo de horarios en Google Drive a un portal web con Django, autenticacion y PostgreSQL.

## Que incluye hoy

- Login con las vistas nativas de Django.
- Navegacion base con panel lateral.
- Modulo de `Sedes`.
- Modulo de `Horarios` para:
  - crear una semana por sede,
  - cargar personal activo desde la fuente externa,
  - editar turnos por trabajador,
  - calcular horas por dia, total semanal y extras,
  - comparar extras calculadas vs horas pendientes cargadas manualmente.
- Configuracion administrativa para:
  - primer dia de la semana,
  - horas semanales por defecto,
  - maximo diario,
  - horas por cargo,
  - catalogo de turnos.
- Soporte para dos bases:
  - `default`: nueva base de horarios,
  - `externa`: `biable01` en `192.168.10.9:5432`.

## Base de datos a crear

La base principal que debe guardar toda la data del portal es:

- Motor: PostgreSQL
- Servidor: el PostgreSQL de tu red o del mismo Debian host
- Puerto: `5432`
- Base de datos: `horarios`
- Usuario recomendado para la aplicacion: `horarios_app`

Ejemplo de creacion:

```sql
CREATE ROLE horarios_app WITH
  LOGIN
  PASSWORD 'cambia_esta_clave';

CREATE DATABASE horarios
  WITH
  OWNER = horarios_app
  ENCODING = 'UTF8';

GRANT ALL PRIVILEGES ON DATABASE horarios TO horarios_app;
```

Variables esperadas en `.env`:

```env
DB_NAME=horarios
DB_USER=horarios_app
DB_PASSWORD=cambia_esta_clave
DB_HOST=192.168.10.9
DB_PORT=5432
```

La aplicacion quedo preparada para trabajar con PostgreSQL como base principal, sin depender de SQLite para la data del portal.

## Hallazgos del sistema actual

Del Excel y del script actual se identifico este flujo:

- La grilla trabaja una semana por sede.
- Cada trabajador tiene dos turnos por dia.
- Las horas diarias salen de la tabla de turnos o se calculan por rango horario.
- Se guardan compensatorios / dias pendientes / horas pendientes.
- El archivo genera consolidado, recargo nocturno, CSV y paz y salvo.

De la base externa se encontraron estas tablas utiles:

- `public.hojas_de_vida`: personal, sede (`id_co`), centro de costo (`id_ccosto`) y cargo (`nombre_cargo`).
- `public.centro_operacion`: catalogo de sedes / CO.
- `public.centro_costo`: catalogo de areas.

Para cargar personal activo por sede, el portal consulta `biable01` desde `contratos`, para no depender de que la persona ya tenga pagos de nomina generados. El cargo toma la descripcion mas reciente disponible en `nmresumen_pagos_nomina`.

Consulta base de personal por sede:

```sql
SELECT DISTINCT
    c.id_co AS id_co_laboral,
    c.id_terc,
    t.nombres,
    t.apellido1,
    c.id_cargo,
    p.descripcion_cargo
FROM contratos c
LEFT JOIN terceros t
    ON TRIM(t.codigo) = TRIM(c.id_terc)
LEFT JOIN (
    SELECT DISTINCT ON (id_contrato)
        id_contrato,
        descripcion_cargo
    FROM nmresumen_pagos_nomina
    ORDER BY id_contrato, lapso_doc DESC, fecha_gen DESC
) p
    ON p.id_contrato = c.codigo
WHERE
    c.estado = 'A'
    AND TRIM(c.id_co) = '007'
ORDER BY
    c.id_co,
    t.apellido1,
    t.nombres;
```

Si una persona no aparece en esa carga automatica, el horario permite agregarla manualmente validando:

- tipo de documento,
- numero de documento,
- nombre completo,
- cargo.

## Pantalla propuesta

La captura web se plantea mas intuitiva que el Excel:

1. El usuario entra al portal con login.
2. En el menu lateral abre `Sedes`.
3. Desde una sede crea o abre un `Horario`.
4. El formulario pide el primer dia de la semana y carga automaticamente el personal activo de esa sede.
5. La grilla muestra:
   - identificacion,
   - empleado,
   - area,
   - cargo,
   - 7 columnas de dias con 2 turnos por dia,
   - horas del dia,
   - total semanal,
   - extras,
   - dias/horas pendientes,
   - diferencia entre lo calculado y lo reportado manualmente.

## Despliegue en Debian con Docker

Este es el enfoque recomendado si quieres poder borrar o recrear la aplicacion sin perder la informacion:

- PostgreSQL queda instalado en Debian o en tu servidor de base de datos fuera del contenedor.
- Django corre dentro de Docker.
- La data vive en la base `horarios`, no dentro del contenedor.

## Guia completa de implantacion

El paso a paso detallado para:

- crear la base `horarios`,
- entender que tablas crea Django,
- conectar GitHub,
- configurar Debian para descargar desde Git,
- separar instalacion inicial y actualizacion normal,
- y dejar el despliegue automatico,

esta en:

- [docs/DESPLIEGUE_DEBIAN_GITHUB.md](/C:/Users/POPULAR/Documents/HORARIOS/docs/DESPLIEGUE_DEBIAN_GITHUB.md)

### 1. Entrar al servidor Debian

```bash
ssh tu_usuario@IP_DEL_SERVIDOR
```

### 2. Instalar Docker y Compose Plugin

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

### 3. Crear la base de datos en PostgreSQL del host

Entrar al motor:

```bash
sudo -u postgres psql
```

Crear usuario y base:

```sql
CREATE ROLE horarios_app WITH LOGIN PASSWORD 'cambia_esta_clave';
CREATE DATABASE horarios OWNER horarios_app ENCODING 'UTF8';
GRANT ALL PRIVILEGES ON DATABASE horarios TO horarios_app;
\q
```

### 4. Permitir conexiones al PostgreSQL del host

Editar `postgresql.conf` y validar:

```conf
listen_addresses = '*'
```

Editar `pg_hba.conf` y agregar una regla para la red Docker local del host:

```conf
host    horarios    horarios_app    172.17.0.0/16    md5
```

Reiniciar PostgreSQL:

```bash
sudo systemctl restart postgresql
sudo systemctl status postgresql
```

### 5. Bajar el proyecto al servidor

```bash
cd /opt
sudo mkdir -p horarios
sudo chown $USER:$USER horarios
cd horarios
git clone <URL_DEL_REPOSITORIO> app
cd app
```

Si no estas usando Git, copia la carpeta del proyecto a `/opt/horarios/app`.

### 6. Crear el archivo `.env`

```bash
cp .env.example .env
nano .env
```

Dejalo asi para Docker en Debian apuntando a tu base `horarios`:

```env
DEBUG=0
SECRET_KEY=cambia-esto-por-una-clave-larga
ALLOWED_HOSTS=127.0.0.1,localhost,IP_DEL_SERVIDOR,tu-dominio-interno
APP_PORT=8000

DB_NAME=horarios
DB_USER=horarios_app
DB_PASSWORD=cambia_esta_clave
DB_HOST=192.168.10.9
DB_PORT=5432

LEGACY_DB_NAME=biable01
LEGACY_DB_USER=biable01
LEGACY_DB_PASSWORD=biable01
LEGACY_DB_HOST=192.168.10.9
LEGACY_DB_PORT=5432

SYNC_CATALOGS_ON_START=0
```

### 7. Construir y levantar la aplicacion

```bash
docker compose build
docker compose up -d
```

El contenedor hace automaticamente:

- espera la base de datos,
- ejecuta `migrate`,
- ejecuta `collectstatic`,
- arranca Django con Gunicorn.

### 8. Crear el usuario administrador

Opcion manual:

```bash
docker compose exec web python manage.py createsuperuser
```

Opcion automatica:

Agrega estas variables al `.env` antes de levantar:

```env
DJANGO_SUPERUSER_USERNAME=admin
DJANGO_SUPERUSER_EMAIL=admin@popular.local
DJANGO_SUPERUSER_PASSWORD=una-clave-segura
```

### 9. Cargar sedes, areas y cargos

```bash
docker compose exec web python manage.py sync_legacy_catalogs
```

Si quieres que lo haga al arrancar, cambia:

```env
SYNC_CATALOGS_ON_START=1
```

### 10. Ver logs y validar

```bash
docker compose logs -f
```

Prueba en navegador:

```text
http://IP_DEL_SERVIDOR:8000
```

### 11. Como actualizar la aplicacion sin perder la data

```bash
cd /opt/horarios/app
git pull
docker compose build
docker compose up -d
```

La informacion no se pierde porque vive en PostgreSQL del host, no en el contenedor.

### 12. Como borrar solo la aplicacion y conservar la base

```bash
docker compose down
```

Si tambien quieres borrar la imagen del app, revisa primero su `IMAGE ID` con `docker images` y luego eliminas esa imagen puntual.

Aunque tumbes o recrees el contenedor, la informacion sigue en la base `horarios` del PostgreSQL del servidor.

## Tablas que crea Django en `horarios`

Despues de `migrate`, Django crea las tablas del portal:

- autenticacion y permisos: `auth_*`, `django_*`
- catalogos del portal: sedes, cargos, areas, turnos, configuracion
- captura operativa: horarios semanales y lineas por trabajador

Relaciones principales:

- `WeeklySchedule` pertenece a una `Site`
- `ScheduleLine` pertenece a un `WeeklySchedule`
- `UserSiteAccess` relaciona usuarios con sedes permitidas
- `JobRole` guarda la jornada parametrizada por cargo
- `ShiftTemplate` guarda los turnos del desplegable

## Siguiente fase recomendada

- Poner Nginx como proxy inverso delante del contenedor en el puerto 80 o 443.
- Mover `ALLOWED_HOSTS` a la URL o IP definitiva de la red empresarial.
- Configurar backups diarios de la base `horarios`.
- Publicar el acceso interno por nombre DNS en vez de IP.
