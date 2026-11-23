# i-MSCP → ISPConfig Migrator

Herramienta de migración, escrita en Python 3, para trasladar servicios de un servidor **i-MSCP** a otro gestionado con **ISPConfig**, conservando los datos y automatizando parte del aprovisionamiento.

> **Estado:** script de uso administrativo, desarrollado y probado para una migración concreta (i-MSCP 1.5.3 en Debian 10 → ISPConfig 3.3.1 en Debian 13). **No es un migrador universal**. Revise el código, las rutas, los esquemas de las bases de datos y los parámetros antes de usarlo en otro entorno. No hay garantía de compatibilidad con otras versiones.

## Objetivos y alcance

El script `migrate.py` permite:

- Inventariar dominios, webs, subdominios, cuentas y redirecciones de correo, bases de datos y registros DNS desde i-MSCP.
- Preparar un plan de migración sin aprovisionar recursos.
- Crear recursos en ISPConfig mediante su **Remote API JSON**: sitios web, dominios y buzones de correo, redirecciones y, opcionalmente, usuarios y bases de datos SQL.
- Transferir contenido web y Maildir mediante `rsync`/SSH.
- Exportar bases de datos MySQL/MariaDB y, si se solicita explícitamente, importarlas **solo en bases de datos vacías**.
- Revisar y, opcionalmente, crear zonas/registros DNS y activar dominios de correo.
- Sincronizar incrementalmente **solo mensajes de correo** con `--sync-mail-only`, sin volver a aprovisionar servicios.

**No realiza** el cambio de DNS público, la actualización de nameservers en el registrador ni una validación completa de las aplicaciones web. Los certificados TLS, la configuración de PHP, DKIM, filtros y otras particularidades deben revisarse después de la migración.

## Arquitectura y requisitos

| Elemento | Origen | Destino |
| --- | --- | --- |
| Sistema de referencia | Debian 10 | Debian 13 |
| Panel de referencia | i-MSCP 1.5.3 | ISPConfig 3.3.1 |
| Ejecución | `root` ejecuta `migrate.py` | Usuario SSH `imscp`, con `sudo` no interactivo |
| Python | Python **3.7 o superior** | No se requiere Python para la transferencia |
| Base de datos | MariaDB/MySQL accesible localmente mediante `mysql` | MariaDB/MySQL, base `dbispconfig` consultable por `sudo mysql` |
| Transferencia | `ssh`, `rsync`, `mysqldump`, `gzip` | `sshd`, `rsync`, `sudo`, `mysql`, `gzip` |
| API | Acceso HTTPS saliente al panel ISPConfig | Remote API JSON habilitada |

El código emplea módulos de la **biblioteca estándar de Python**; no requiere instalar paquetes de Python con `pip`.

### Requisitos en el origen (Debian 10)

1. Ejecutar como **`root`** (`os.geteuid() == 0`).
2. Acceso a las tablas de i-MSCP y a las tablas de autenticación SQL utilizadas por el migrador mediante el cliente `mysql` local, sin petición interactiva de contraseña. Revisar las credenciales de `/root/.my.cnf` o el mecanismo local existente.
3. Acceso de lectura a las rutas de los sitios y buzones originales, habitualmente:
   - `/var/www/virtual/DOMINIO/htdocs`
   - `/var/mail/virtual/DOMINIO/USUARIO` (Maildir con `cur/`, `new/` y `tmp/`; también se reconoce `Maildir/` dentro de esta ruta).
4. Cliente OpenSSH y `rsync` instalados. Para SQL, `mysqldump` y `gzip`.
5. Espacio local suficiente para los volcados SQL comprimidos, cuando se use `--include-sql`.
6. Conectividad SSH al destino y HTTPS al endpoint de la API de ISPConfig.

### Requisitos en el destino (Debian 13)

1. ISPConfig operativo, con el cliente (`client_id`), servidor (`server_id`) e IP de publicación adecuados.
2. Usuario Unix **`imscp`** con directorio personal `/home/imscp`, acceso SSH mediante clave pública y permisos de escritura en `/home/imscp/imscp-migration-staging/`.
3. `sudo` **sin contraseña** para los comandos remotos requeridos (véase el apartado de seguridad). El script invoca `sudo -n`, por lo que **no puede introducir una contraseña de sudo**.
4. `rsync` y `mysql` instalados. `sudo mysql -N -B --raw dbispconfig -e '...'` debe funcionar para consultar la base de datos de ISPConfig.
5. Las cuentas de correo existentes deben tener `maildir_format='maildir'` y la ruta Maildir de Dovecot bajo `MAIL_HOME/Maildir/`.
6. La cuenta de API remota debe disponer de permisos para las operaciones de sitios, correo, bases de datos y DNS que se vayan a utilizar.

## Configuración del entorno (v9.9)

La versión 9.9 **no incluye dominios, direcciones IP, nombres de servidores ni exclusiones de ninguna migración real**. Los parámetros se indican mediante opciones CLI o mediante un archivo JSON local.

Copiar `config.example.json` como `config.json` y adaptar los valores:

```bash
cp config.example.json config.json
chmod 600 config.json
```

El ejemplo usa dominios reservados (`example.net`) y la IP de documentación `192.0.2.10`. Sustituirlos por valores reales antes de ejecutar. El archivo `config.json` debe permanecer fuera de Git.

```json
{
  "host": "mail.example.net",
  "api-url": "https://mail.example.net:8080/remote/json.php",
  "api-user": "migration_api",
  "client-id": 1,
  "server-id": 1,
  "ip": "192.0.2.10",
  "ssh-user": "imscp",
  "ssh-key": "/root/.ssh/id_rsa",
  "exclude-domain": [],
  "exclude-database": [],
  "skip-web-domain": [],
  "mail-merge": []
}
```

Las listas permiten excluir dominios completos, bases de datos, o solo la web principal de un dominio. `mail-merge` admite pares `origen@dominio=destino@dominio` para dejar un buzón especial preparado en staging y fusionarlo **manualmente**, sin sobrescribir mensajes. No se fusiona automáticamente.

```bash
python3 migrate.py --config config.json --inventory
python3 migrate.py --config config.json --plan --domain example.net
```

Los parámetros escalares indicados explícitamente en CLI prevalecen sobre los del JSON. No se almacena la contraseña de API en el JSON: se solicita de forma interactiva o mediante `ISPC_API_PASSWORD`. Las rutas de i-MSCP, ISPConfig y staging siguen reflejando la estructura de referencia del programa; revisarlas si difieren en otro despliegue.

## Autenticación SSH con clave RSA

Por defecto, el script usa la clave privada **`/root/.ssh/id_rsa`** del servidor origen, con `IdentitiesOnly=yes` y `BatchMode=yes`. Esto exige autenticación **sin interacción**: la clave debe ser utilizable sin solicitar una frase de paso durante la ejecución, o debe estar disponible mediante un agente compatible con la sesión de `root`.

En el **origen**, si aún no existe una clave apropiada:

```bash
sudo -i
mkdir -p /root/.ssh
chmod 700 /root/.ssh
ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C 'imscp-migration'
```

Se recomienda proteger la clave privada con una frase de paso y utilizar un agente SSH cuando el entorno lo permita. **Nunca subir la clave privada ni las credenciales al repositorio**. Si ya existe `/root/.ssh/id_rsa`, no sobrescribirla: reutilizarla o configurar `ssh-key` en el JSON (o `--ssh-key` en CLI).

En el **destino**, crear el usuario si todavía no existe:

```bash
sudo adduser --disabled-password --gecos '' imscp
sudo install -d -m 700 -o imscp -g imscp /home/imscp/.ssh
```

Copiar **únicamente la clave pública** del origen (`/root/.ssh/id_rsa.pub`) a `/home/imscp/.ssh/authorized_keys` del destino. Por ejemplo, desde el origen, si se dispone de un acceso administrativo inicial:

```bash
ssh-copy-id -i /root/.ssh/id_rsa.pub imscp@DESTINO
```

Si el usuario todavía no admite autenticación con contraseña, instalar la clave pública mediante una sesión administrativa existente. Después, en el destino:

```bash
sudo chown -R imscp:imscp /home/imscp/.ssh
sudo chmod 700 /home/imscp/.ssh
sudo chmod 600 /home/imscp/.ssh/authorized_keys
```

Comprobar desde el origen:

```bash
ssh -i /root/.ssh/id_rsa -o IdentitiesOnly=yes -o BatchMode=yes \
  imscp@DESTINO 'id && hostname'
```

La primera conexión debe tener la huella del servidor verificada y registrada en `known_hosts` antes de automatizar las operaciones.

## Permisos sudoers en el destino

**La versión 9.9 no está preparada para funcionar con una regla `sudoers` limitada únicamente a `rsync` y `mysql`.** Para las consultas y operaciones remotas ejecuta literalmente:

```text
sudo -n sh -c '<comando>'
```

Y, en la sincronización incremental, `rsync` utiliza:

```text
--rsync-path='sudo -n rsync'
```

Por tanto, para ejecutar el código **sin modificarlo**, el usuario remoto necesita privilegios suficientemente amplios. En un servidor **dedicado y de confianza**, la configuración funcional más sencilla es:

```bash
sudo visudo -f /etc/sudoers.d/imscp-migration
```

Contenido:

```sudoers
imscp ALL=(root) NOPASSWD: ALL
```

Validar y proteger el archivo:

```bash
sudo chmod 440 /etc/sudoers.d/imscp-migration
sudo visudo -cf /etc/sudoers.d/imscp-migration
```

Verificar desde el origen:

```bash
ssh -i /root/.ssh/id_rsa -o BatchMode=yes imscp@DESTINO \
  'sudo -n sh -c "id -u" && sudo -n rsync --version | head -n 1'
```

**Advertencia de seguridad:** `NOPASSWD: ALL` otorga capacidad de administración completa al usuario `imscp` y, por extensión, a quien posea su clave SSH. Debe ser una autorización **temporal**, limitada a la ventana de migración. Proteger la clave, restringir el acceso SSH por IP cuando sea posible, registrar la actividad y **eliminar `/etc/sudoers.d/imscp-migration` al terminar**. Para una distribución pública más segura habría que refactorizar las llamadas remotas para que utilicen un conjunto explícito de comandos privilegiados y un wrapper controlado; **no** se debe presentar una regla limitada como compatible con el código actual.

## Configuración de la API de ISPConfig

Crear en ISPConfig un usuario de **Remote API** con los permisos necesarios. La contraseña se obtiene de la variable de entorno `ISPC_API_PASSWORD` o, si no existe, se solicita interactivamente mediante `getpass`.

```bash
# Ejemplo: se pedirá la contraseña de API al ejecutar --apply
python3 migrate.py --config config.json --apply --domain example.net \
  --host DESTINO \
  --api-url https://DESTINO:8080/remote/json.php \
  --api-user USUARIO_API --client-id 1 --server-id 1 --ip IP_DESTINO \
  --confirm-apply
```

La conexión API exige **HTTPS** y verifica el certificado TLS. No se debe desactivar esta comprobación para evitar problemas de certificado: instalar una cadena de confianza válida y usar el nombre DNS correcto.

`--sync-mail-only` **no utiliza la API**, pero sí necesita acceso SSH y consultas SQL remotas a `dbispconfig`.

## Opciones de línea de comandos

Consultar siempre la ayuda de la versión instalada:

```bash
python3 migrate.py --help
```

| Opción | Descripción |
| --- | --- |
| `--inventory` | Inventaría recursos y guarda `inventory.json`, sin aprovisionar ni transferir. |
| `--plan` | Muestra el plan de migración sin aplicarlo. |
| `--apply` | Ejecuta el aprovisionamiento y las transferencias solicitadas. |
| `--sync-mail-only` | Sincronización incremental directa de buzones existentes; no modifica webs, SQL, DNS ni recursos de API. |
| `--domain DOMINIO` | Selecciona un único dominio. |
| `--all` | Selecciona todos los dominios activos no excluidos. |
| `--host HOST` | Host SSH de destino. |
| `--api-url URL` | Endpoint JSON de ISPConfig. |
| `--api-user USUARIO` | Usuario de Remote API. |
| `--config ARCHIVO` | Lee parámetros del entorno desde un JSON local. |
| `--ssh-user USUARIO` | Usuario SSH del destino; por defecto `imscp`. |
| `--ssh-key RUTA` | Ruta de clave SSH privada; por defecto `/root/.ssh/id_rsa`. |
| `--exclude-domain DOMINIO` | Excluye un dominio y todos sus recursos; repetible. |
| `--exclude-database BD` | Excluye una base de datos SQL; repetible. |
| `--skip-web-domain DOMINIO` | Omite la web principal de ese dominio; repetible. |
| `--mail-merge ORIGEN=DESTINO` | Prepara un buzón especial para fusión manual; repetible. |
| `--client-id ID` | ID de cliente de ISPConfig. |
| `--server-id ID` | ID del servidor de ISPConfig. |
| `--ip IP` | Dirección IP para nuevos sitios web. |
| `--skip-web` | Omite el aprovisionamiento y transferencia de webs. |
| `--skip-mail` | Omite el aprovisionamiento y transferencia de correo. |
| `--include-sql` | Incluye creación de recursos SQL y exportación de datos. |
| `--import-sql` | Importa los volcados SQL; exige `--include-sql` y una base de datos de destino vacía. |
| `--include-dns` | Incluye los registros DNS en el plan; por sí solo no escribe DNS. |
| `--dns-apply` | Aplica cambios DNS; exige `--include-dns`. |
| `--create-dns-zones` | Crea zonas DNS ausentes; exige `--dns-apply`, `--dns-ns1` y `--dns-ns2`. |
| `--dns-ns1 FQDN` | Nameserver primario para zonas nuevas. |
| `--dns-ns2 FQDN` | Nameserver secundario para zonas nuevas. |
| `--activate-mail` | Activa dominios de correo en ISPConfig; no cambia MX públicos. |
| `--api-only` | Aprovisiona recursos sin transferir contenido. |
| `--confirm-apply` | Confirmación obligatoria para operaciones reales. |
| `--dry-run` | Simula la sincronización de `--sync-mail-only`, sin transferir archivos. |

`--inventory`, `--plan`, `--apply` y `--sync-mail-only` son **modos mutuamente excluyentes**. `--dry-run` solo se admite con `--sync-mail-only`. Las opciones de aprovisionamiento, DNS y SQL no pueden combinarse con `--sync-mail-only`.

## Flujo de funcionamiento

1. **Descubrimiento:** consulta las tablas de i-MSCP y genera un inventario local en `/root/imscp-migration/inventory.json`.
2. **Filtrado:** aplica la selección de dominio y las exclusiones configuradas en el código.
3. **Plan:** identifica sitios, buzones y bases de datos; puede mostrar registros DNS si se solicita.
4. **Aprovisionamiento:** utiliza la API de ISPConfig para crear los recursos necesarios, comprobando antes si existen.
5. **Transferencia inicial:** copia los datos mediante `rsync` a `/home/imscp/imscp-migration-staging/` y después los instala en las rutas de ISPConfig mediante comandos remotos con `sudo`.
6. **SQL opcional:** genera archivos `.sql.gz` en el origen, los transfiere y puede importarlos en bases vacías. Registra un marcador SHA-256 para detectar importaciones ya realizadas.
7. **DNS opcional:** revisa y crea zonas/registros bajo autorización explícita; no cambia DNS público.
8. **Sincronización posterior de correo:** `--sync-mail-only` evita staging y copia directamente mensajes pendientes a los Maildir existentes del destino.

### Tratamiento de buzones

En la instalación de referencia, ISPConfig almacena el **directorio de inicio** del buzón en `mail_user.maildir`, pero Dovecot utiliza realmente:

```text
/var/vmail/DOMINIO/USUARIO/Maildir/
```

El migrador consulta `maildir` y `maildir_format` en `dbispconfig` para obtener el destino. Solo acepta `maildir_format='maildir'`. Excluye índices y metadatos específicos de Dovecot/Courier, Sieve y otros archivos que no son mensajes. Los archivos existentes en destino no se sobrescriben.

La sincronización incremental de la versión 9.9 utiliza `rsync` con `--ignore-existing`, `--inc-recursive`, `--whole-file` y `--no-compress`. **No utiliza `--checksum`** para comparar el contenido de los mensajes ya existentes. Aun así, `rsync` debe recorrer los directorios y enumerar los nombres de archivo. La coincidencia por nombre no garantiza detectar duplicados si un mismo mensaje tiene nombres distintos.

> **Atención:** el modo `--sync-mail-only` exige que los buzones ya existan y tengan una estructura Maildir válida en destino. No crea buzones ni corrige migraciones antiguas realizadas con una ruta Maildir equivocada. Si se ejecutó una versión anterior afectada por ese problema, reparar primero la estructura en destino.

## Procedimiento de prueba recomendado

### 1. Instalar y revisar

En el origen:

```bash
sudo -i
mkdir -p /root/imscp_migration
cd /root/imscp_migration
# Copiar migrate.py y config.example.json a este directorio; crear config.json.
python3 --version
python3 -m py_compile migrate.py
python3 migrate.py --help
```

Verificar la conectividad SSH, `sudo` y la API antes de intentar operaciones reales.

### 2. Generar inventario (sin cambios remotos)

```bash
python3 migrate.py --config config.json --inventory
```

Revisar `/root/imscp-migration/inventory.json`. **No subir ese inventario a GitHub**: puede contener nombres internos y otros datos de infraestructura.

### 3. Revisar el plan de un dominio

```bash
python3 migrate.py --config config.json --plan --domain example.net
```

Para incluir la revisión DNS:

```bash
python3 migrate.py --config config.json --plan --domain example.net --include-dns
```

### 4. Aprovisionar y transferir un dominio de prueba

```bash
python3 migrate.py --config config.json --apply --domain example.net \
  --host DESTINO --api-url https://DESTINO:8080/remote/json.php \
  --api-user USUARIO_API --client-id 1 --server-id 1 --ip IP_DESTINO \
  --confirm-apply
```

En este ejemplo **no** se importan bases de datos ni se aplican cambios DNS, porque no se han indicado sus opciones.

Comprobar en ISPConfig el sitio, buzones, redirecciones, propietarios y permisos de los archivos, y revisar las rutas de Maildir antes de migrar todos los dominios.

### 5. Migrar todos los dominios con SQL y DNS (opcional)

**Solo después de validar el dominio de prueba y revisar las exclusiones:**

```bash
python3 migrate.py --config config.json --apply --all \
  --include-sql --import-sql \
  --include-dns --dns-apply --create-dns-zones \
  --dns-ns1 ns1.ejemplo.net --dns-ns2 ns2.ejemplo.net \
  --activate-mail --confirm-apply
```

Los nombres de servidores DNS son ejemplos: sustituirlos por los reales. Esta operación **sí modifica** ISPConfig e importa datos SQL. Las importaciones SQL solo se admiten en bases de datos vacías.

### 6. Simular la sincronización incremental de correo

```bash
python3 migrate.py --config config.json --sync-mail-only --domain example.net --dry-run
```

No aprovisiona recursos ni transfiere archivos. Permite revisar qué mensajes se copiarían.

### 7. Sincronizar correo de un dominio

```bash
python3 migrate.py --config config.json --sync-mail-only --domain example.net --confirm-apply
```

### 8. Sincronizar correo de todos los dominios

```bash
python3 migrate.py --config config.json --sync-mail-only --all --confirm-apply
```

Este modo transfiere directamente de origen a destino, **sin staging**, sin `--delete` y sin sobrescribir archivos existentes. Se puede repetir para incorporar nuevos mensajes. No es una sincronización bidireccional ni replica eliminaciones o modificaciones de mensajes ya existentes.

### 9. Cambio definitivo de servicio

Antes de cambiar MX/nameservers y activar el tráfico definitivo:

1. Confirmar que todos los buzones y destinos existen y que los servicios de correo funcionan.
2. Coordinar una ventana de corte y detener o desviar la entrega de correo nuevo en el origen.
3. Ejecutar una **última** sincronización `--sync-mail-only --all --confirm-apply`.
4. Comprobar el resultado y después realizar el cambio de DNS público y de servicios.
5. Mantener el origen disponible durante el período de verificación y retirar los permisos temporales de migración al finalizar.

## Limitaciones y precauciones

- **No es una herramienta de rollback.** Preparar un procedimiento de recuperación antes de migrar servicios de producción.
- El script presupone esquemas de tablas y rutas específicos de i-MSCP e ISPConfig. Revisarlos para otras versiones.
- El modo `--apply` realiza operaciones reales y necesita confirmación. El modo `--plan` no sustituye una prueba controlada.
- Las contraseñas de buzones y usuarios SQL se migran a partir de hashes existentes; se admiten únicamente los formatos contemplados en el código.
- Las carpetas IMAP y los nombres de mensajes se preservan, pero deben comprobarse los permisos y el acceso real desde Dovecot.
- Algunos registros DNS heredados, especialmente DKIM/ADSP, se omiten deliberadamente; configurar DKIM y revisar SPF/DMARC en el servidor nuevo.
- Las cuentas especiales que requieren fusionar buzones no se fusionan automáticamente.
- Las webs pueden requerir ajustes de versiones de PHP, módulos, permisos, configuraciones y certificados.
- No ejecutar simultáneamente varias instancias que modifiquen los mismos recursos o buzones.
- No subir a GitHub claves SSH privadas, contraseñas, volcados SQL, inventarios ni registros con datos sensibles.

## Publicación del repositorio

Estructura mínima sugerida:

```text
imscp-ispconfig-migrator/
├── migrate.py
├── README.md
├── config.example.json
└── .gitignore
```

Contenido recomendado de `.gitignore`:

```gitignore
__pycache__/
*.pyc
.env
*.sql
*.sql.gz
*.pem
id_rsa*
inventory.json
*.log
config.json
```

**Licencia:** añadir un archivo `LICENSE` solo después de decidir los términos de distribución. No se presupone una licencia de código abierto por el mero hecho de publicar el repositorio.

---

**Versión documentada:** 9.9. El código del migrador es la referencia definitiva para las opciones y operaciones disponibles.


### Propiedad de los Maildir en sincronizaciones incrementales (v9.10)

La sincronizacion `--sync-mail-only` usa `rsync -a --chown=vmail:vmail` con `sudo -n rsync` en destino. No se combinan `--no-owner` ni `--no-group`, porque anularian `--chown` y podrian crear directorios `root:root`, inaccesibles para Dovecot. El destino debe disponer del usuario y grupo `vmail`.

La correccion se aplica a archivos y directorios procesados por rsync; si una ejecucion anterior dejo archivos existentes con propietario incorrecto, `--ignore-existing` no los corrige. En ese caso, revisar y reparar una sola vez la propiedad de los Maildir afectados en el servidor destino, por ejemplo con `sudo chown -R vmail:vmail /var/vmail/example.net/user/Maildir`.
