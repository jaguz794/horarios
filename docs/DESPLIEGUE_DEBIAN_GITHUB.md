# Despliegue en Debian, PostgreSQL y GitHub

## 1. Base de datos principal

La aplicacion ya quedo configurada para usar como base principal:

```env
DB_NAME=horarios
DB_USER=horarios_app
DB_PASSWORD=horarios901768321*.+
DB_HOST=192.168.10.9
DB_PORT=5432
```

La app ya no debe depender de SQLite para la base principal.

## 2. Que debes crear en PostgreSQL

No debes crear las tablas a mano una por una. Debes:

1. Crear el usuario `horarios_app`
2. Crear la base `horarios`
3. Dar permisos
4. Ejecutar `python manage.py migrate`

### 2.1. Entrar a PostgreSQL

```bash
sudo -u postgres psql
```

### 2.2. Crear usuario y base

```sql
CREATE ROLE horarios_app WITH LOGIN PASSWORD 'horarios901768321*.+';
CREATE DATABASE horarios OWNER horarios_app ENCODING 'UTF8';
GRANT ALL PRIVILEGES ON DATABASE horarios TO horarios_app;
\q
```

### 2.3. Permitir conexiones si la base esta en otro servidor o por red

En `postgresql.conf`:

```conf
listen_addresses = '*'
```

En `pg_hba.conf` agrega la red o IP desde donde se conectara el portal:

```conf
host    horarios    horarios_app    192.168.10.0/24    md5
```

Reinicia PostgreSQL:

```bash
sudo systemctl restart postgresql
```

## 3. Tablas que crea Django en la base `horarios`

Cuando ejecutes migraciones, Django crea estas tablas principales del portal:

### 3.1. Tablas de Django

- `auth_user`
- `auth_group`
- `auth_permission`
- `auth_user_groups`
- `auth_user_user_permissions`
- `django_admin_log`
- `django_content_type`
- `django_migrations`
- `django_session`

### 3.2. Tablas del modulo `core`

- `sedes`
- `areas`
- `parametros_cargos`
- `catalogo_turnos`
- `configuracion_sistema`
- `accesos_usuario_sede`
- `accesos_usuario_sede_sedes`

### 3.3. Tablas del modulo `schedules`

- `horarios_semanales`
- `horarios_detalle`

## 4. Relacion entre tablas

- `accesos_usuario_sede.user_id` apunta a `auth_user.id`
- `accesos_usuario_sede_sedes.usersiteaccess_id` apunta a `accesos_usuario_sede.id`
- `accesos_usuario_sede_sedes.site_id` apunta a `sedes.id`
- `horarios_semanales.site_id` apunta a `sedes.id`
- `horarios_semanales.created_by_id` apunta a `auth_user.id`
- `horarios_semanales.updated_by_id` apunta a `auth_user.id`
- `horarios_detalle.schedule_id` apunta a `horarios_semanales.id`

## 5. Orden real de creacion

El orden correcto no es manual. Debes dejar que Django lo haga:

```bash
python manage.py migrate
```

Ese comando crea:

1. Tablas de autenticacion y sistema de Django
2. Tablas funcionales del portal en espanol
3. Tablas de horarios y su detalle
4. Restricciones, indices y llaves foraneas

## 6. Archivo `.env` del servidor

Crea `/opt/horarios/app/.env` con este contenido:

```env
DEBUG=0
SECRET_KEY=cambia-esta-clave
ALLOWED_HOSTS=127.0.0.1,localhost,IP_DEL_SERVIDOR
APP_PORT=8000

DB_NAME=horarios
DB_USER=horarios_app
DB_PASSWORD=horarios901768321*.+
DB_HOST=192.168.10.9
DB_PORT=5432

LEGACY_DB_NAME=biable01
LEGACY_DB_USER=biable01
LEGACY_DB_PASSWORD=biable01
LEGACY_DB_HOST=192.168.10.9
LEGACY_DB_PORT=5432

SYNC_CATALOGS_ON_START=0
DJANGO_SUPERUSER_USERNAME=
DJANGO_SUPERUSER_EMAIL=
DJANGO_SUPERUSER_PASSWORD=
```

## 7. Subir el proyecto a GitHub

En esta sesion no hay autenticacion activa con GitHub y no existe remoto configurado, por eso el proyecto no se puede subir automaticamente desde aqui todavia. El flujo correcto es este:

### 7.1. Crear el repositorio en GitHub

Hazlo desde la web de GitHub:

1. Entra a GitHub
2. Crea un repositorio nuevo
3. Ejemplo: `horarios`
4. No subas `.env`

### 7.2. Conectar el proyecto local con GitHub

Desde tu equipo local:

```bash
git init
git add .
git commit -m "Inicializa portal de horarios"
git branch -M master
git remote add origin https://github.com/TU_USUARIO/horarios.git
git push -u origin master
```

Si prefieres SSH:

```bash
git remote add origin git@github.com:TU_USUARIO/horarios.git
git push -u origin master
```

### 7.3. Dejar el push automatico despues de cada commit

En tu equipo local, dentro del proyecto:

```bash
git config core.hooksPath .githooks
git config push.autoSetupRemote true
```

Con eso, cada vez que se cree un commit local, el hook:

- detecta la rama actual
- ejecuta `git push origin rama_actual`
- deja a GitHub Actions disparar la actualizacion del servidor

Si quieres publicar manualmente con un solo comando, usa:

```powershell
.\scripts\publicar.ps1 -Mensaje "Describe el cambio"
```

Si no mandas mensaje, el script genera uno con fecha y hora.

## 8. Preparar el servidor Debian para bajar desde Git

### 8.1. Instalar Git y Docker

```bash
sudo apt update
sudo apt install -y git ca-certificates curl gnupg
```

Si todavia no tienes Docker:

```bash
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

### 8.2. Crear carpeta del proyecto

```bash
sudo mkdir -p /opt/horarios
sudo chown $USER:$USER /opt/horarios
cd /opt/horarios
```

### 8.3. Configurar clave SSH en el servidor

```bash
ssh-keygen -t ed25519 -C "deploy-horarios"
cat ~/.ssh/id_ed25519.pub
```

Copia esa llave publica y agregala en GitHub:

- Opcion A: como `Deploy key` del repositorio
- Opcion B: en tu usuario GitHub, dentro de `SSH keys`

Prueba acceso:

```bash
ssh -T git@github.com
```

### 8.4. Clonar el repositorio

```bash
git clone git@github.com:TU_USUARIO/horarios.git app
cd app
```

### 8.5. Crear el `.env`

```bash
nano .env
```

Pega el `.env` del punto 6.

## 9. Levantar la aplicacion en el servidor

```bash
docker compose build
docker compose up -d
```

El contenedor:

- espera la base `horarios`
- ejecuta `migrate`
- ejecuta `collectstatic`
- arranca Gunicorn

## 10. Flujo separado y automatizado

### 10.1. Instalacion inicial

Haz esto solo la primera vez, o cuando montes un servidor nuevo:

```bash
cd /opt/horarios/app
chmod +x scripts/servidor_instalacion_inicial.sh scripts/servidor_actualizar.sh
./scripts/servidor_instalacion_inicial.sh /ruta/al/turnos.xlsx
```

Ese script hace:

1. levanta o reconstruye la aplicacion
2. sincroniza sedes, areas y cargos desde la base externa
3. copia el Excel de turnos al contenedor
4. importa los turnos al catalogo

### 10.2. Actualizacion normal

Para actualizaciones del portal sin recargar catalogos:

```bash
cd /opt/horarios/app
./scripts/servidor_actualizar.sh master
```

Ese script hace:

1. `git fetch`
2. `git pull`
3. `docker compose up -d --build`

## 11. Crear el administrador

```bash
docker compose exec web python manage.py createsuperuser
```

## 12. Cargar catalogos manualmente cuando sea necesario

```bash
docker compose exec web python manage.py sync_legacy_catalogs
```

Y si cambian los turnos:

```bash
docker cp /ruta/al/turnos.xlsx $(docker compose ps -q web):/tmp/turnos.xlsx
docker compose exec web python manage.py import_shift_templates --file /tmp/turnos.xlsx
```

## 13. Como desplegar cambios nuevos

### 13.1. Flujo local hacia GitHub

En local:

```bash
git add .
git commit -m "Describe el cambio"
git push origin master
```

O con el script del proyecto en Windows:

```powershell
.\scripts\publicar.ps1 -Mensaje "Describe el cambio"
```

En servidor:

```bash
cd /opt/horarios/app
git pull origin master
docker compose up -d --build
```

### 13.2. Flujo automatico con GitHub Actions

El repo ya quedo con este workflow:

- `.github/workflows/deploy.yml`

Y con estos scripts:

- `scripts/servidor_actualizar.sh`
- `scripts/servidor_instalacion_inicial.sh`

Para activarlo debes crear estos secretos en GitHub:

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_PORT`
- `DEPLOY_SSH_KEY`

Luego, cada `push` a `master` o `main` ejecuta:

1. conexion SSH al servidor
2. `git fetch`
3. `git pull`
4. `docker compose up -d --build`

## 14. Verificaciones recomendadas

En servidor:

```bash
docker compose logs -f
docker compose ps
docker compose exec web python manage.py check
```

Validar tablas creadas:

```bash
docker compose exec web python manage.py showmigrations
```

Si todo esta aplicado, deben salir con `[X]`.
