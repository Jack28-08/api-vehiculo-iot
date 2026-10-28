# API Vehículo IoT — práctica 2.5

API con FastAPI, POO y los **21 procedimientos almacenados** existentes de `vehiculo_iot` en Amazon RDS. Incluye siete GET de negocio, trece POST originales, tres POST de control y dos GET de salud (25 operaciones HTTP), además del WebSocket por vehículo. Los eventos confirman operaciones de RDS; no acreditan actuación física del microcontrolador.

## Programación orientada a objetos: código y demostración

La [guía de POO](docs/poo.md) permite seguir una petición de Swagger hasta las
clases que ejecutan la operación. Incluye el diagrama de clases, una demostración
local y un recorrido para la revisión del código.

| Concepto | Implementación utilizada por la API |
| --- | --- |
| Objetos del dominio | [`Vehiculo`, `Comando` y `Demo`](app/domain/vehiculos.py) contienen reglas de actividad y pertenencia; sus datos son inmutables. |
| Controladores | [`VehiculoController`](app/routes/vehiculos.py), [`DemoController`](app/routes/demos.py), [`HealthController`](app/routes/health.py) y [`WebSocketController`](app/routes/websocket.py) registran sus métodos enlazados en FastAPI. |
| Abstracción | [`Movimiento`](app/services/movimientos.py) es una clase abstracta con un algoritmo común; `RepositorioMovimientos` declara las operaciones de datos que necesita. |
| Herencia y polimorfismo | `MovimientoIndividual` y `MovimientoDeDemo` heredan de `Movimiento` y sobrescriben la política de historial. La ejecución individual registra historial; el paso DEMO no lo consulta ni lo inserta. Ambas clases se utilizan en los servicios reales. |
| Encapsulación y composición | Los movimientos exponen sus entidades mediante propiedades; las entidades son inmutables. Cada servicio utiliza un gestor y un repositorio, y cada controlador administra su router. |

Para comprobar las mismas clases de movimiento con un repositorio en memoria,
sin iniciar AWS ni cargar credenciales:

```powershell
.\.venv\Scripts\python.exe -m scripts.demostrar_poo
```

La salida identifica los dos tipos de movimiento: después de ambos hay **un solo
registro de historial**. Es una demostración local de polimorfismo, separada de
las pruebas públicas contra RDS. La refactorización conserva las rutas,
permisos, cuerpos, respuestas y procedimientos del contrato existente.

## Preparar el entorno en PowerShell

Requisitos: Python 3.14.8 y Git. Desde la carpeta del proyecto:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Edita `.env` localmente con `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD` y `MYSQL_DATABASE=vehiculo_iot`. Las credenciales permanecen fuera del código y de Git. Los comandos utilizan el Python del entorno virtual sin necesidad de activarlo. El repositorio publica el contrato y las evidencias previamente obtenidas; no incluye una base de datos local ni crea el esquema de RDS.

Prepara las claves de API:

```powershell
.\.venv\Scripts\python.exe -m scripts.ensure_api_keys
```

El script genera `API_KEY_OPERATOR` y `API_KEY_ADMIN` aleatorias si faltan o están vacías. Conserva los valores no vacíos existentes y los demás bytes del archivo; únicamente informa los nombres preparados. Las variables de entorno tienen prioridad sobre `.env`; si alguna está vacía y oculta el archivo, el script lo advierte. Reinicia el servidor después de cambiar claves o configuración, porque su lectura se almacena en caché. `.env.example` contiene claves vacías y `.env` está excluido de Git.

## Ejecutar y comprobar

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Abre [Swagger UI](http://127.0.0.1:8000/docs). El servidor escucha exclusivamente en este equipo; detén el proceso con `Ctrl+C`. Usa un solo worker: la coordinación, las tareas y los monitores son locales a ese proceso. Todos los GET son públicos. Para los POST, usa **Authorize** e introduce una clave desde el `.env` local en el esquema `ApiKey`, que envía el encabezado `X-API-Key`; evita mostrar o imprimir las claves. Operador y administrador pueden ejecutar movimientos, iniciar/cancelar DEMO y detener lógicamente. La gestión de recursos sigue reservada al administrador.

En otra terminal:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/health
Invoke-RestMethod -Uri http://127.0.0.1:8000/health/db
(Invoke-WebRequest -Uri http://127.0.0.1:8000/docs).StatusCode
Invoke-RestMethod -Uri http://127.0.0.1:8000/vehiculos
```

`/health` comprueba la API. `/health/db` ejecuta `SELECT 1` real y responde `200` con `{"status":"ok"}` si conecta, o `503` con un mensaje fijo si RDS no está disponible. Las rutas MySQL son síncronas (`def`) y FastAPI las ejecuta en hilos; cada caso de uso obtiene una conexión independiente.

## Contrato, arquitectura y verificaciones

La inspección previa real encontró **6 tablas, 34 columnas, 18 restricciones PK/FK/UNIQUE, 21 SP, 52 parámetros y las 21 definiciones disponibles**. El contrato se conserva en [JSON](docs/contrato_rds.json) y [Markdown](docs/contrato_rds.md). Para repetir la inspección de metadatos, sin ejecutar SP ni modificar la base:

```powershell
.\.venv\Scripts\python.exe -m scripts.inspect_rds
```

El [mapa de endpoints y SP](docs/mapa_endpoints_sp.md) detalla las 21 correspondencias. La [guía de arquitectura HTTP](docs/arquitectura_http.md) explica las clases, validaciones, transacciones, resultados con JOIN y decisiones de concurrencia. El historial HTTP devuelve como máximo diez registros; todos permanecen en RDS. Las actualizaciones mediante POST reemplazan los campos editables completos; los opcionales omitidos se escriben como `NULL`.

Ejecuta las pruebas aisladas, sin necesitar conexión a RDS. Se conservan las **124 del baseline 2.5** y se añaden las comprobaciones de preparación de 2.6:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Solo cuando RDS esté disponible y el servidor iniciado, puedes ejecutar las comprobaciones reales de lectura:

```powershell
$marcaVerificacionIoT = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
.\.venv\Scripts\python.exe -m scripts.check_http_reads --base-url http://127.0.0.1:8000 --output ".\evidencias\get-$marcaVerificacionIoT.json"
.\.venv\Scripts\python.exe -m scripts.check_websocket_reads --base-url http://127.0.0.1:8000 --output ".\evidencias\ws-$marcaVerificacionIoT.json"
```

Las pruebas aisladas utilizan dobles locales, incluidas autenticación, pertenencia, reversión del movimiento si falla el segundo SP y guardado del verificador. El script HTTP realiza GET contra la API y RDS; no ejecuta POST. `/health` y `/docs` pueden utilizarse con RDS detenido; las operaciones que necesitan la base responden `503` cuando no está disponible.

Las primeras comprobaciones reales realizaron 13 GET y cubrieron los siete tipos de consulta de negocio, `/health`, `/health/db` y `/docs` con HTTP `200`, además de `404` para un vehículo inexistente. El monitor de lectura comprobó dos clientes simultáneos, reconexión y cierre `4404`. Esas comprobaciones no enviaron POST ni escribieron en RDS. Evidencias: [GET](docs/verificacion_http_get.json) y [WebSocket de lectura](docs/verificacion_websocket_lectura.json).

La **verificación posterior de DEMO se realizó con dos clientes WebSocket reales de Python**, mediante la biblioteca `websockets`; no con Thunder Client. La [evidencia completa](docs/verificacion_demo_real_completa.json) registra un único POST de inicio para esa ejecución, HTTP `202`, su UUID y, en ambos clientes, estado inicial, `demo_inicio`, ocho movimientos y `demo_fin`. Orden, comandos, duración y pausa coinciden con los movimientos consultados en RDS. Se comprobaron las esperas mínimas de cada paso, incluida la última antes de `demo_fin`; el tiempo observado incluye el acceso a RDS y la red. Las seis filas completas del historial, último ID `6`, permanecieron idénticas antes y después. Esa ejecución no envió un POST de movimiento individual y solo actualizó el estado mediante la DEMO.

También se conserva el [intento anterior incompleto](docs/verificacion_demo_real.json), cuyo guardado falló con `PermissionError` y no capturó `demo_fin`. El [diagnóstico de archivos](docs/diagnostico_verificador_io.json) reproduce el bloqueo de reemplazo en Windows. El verificador corregido comprueba creación, escritura y lectura del archivo antes del POST y acumula los eventos en memoria hasta terminar la escucha. Su opción `--new-execution` **inicia una DEMO real y actualiza el estado**: no forma parte de las pruebas aisladas ni es necesaria para consultar las evidencias publicadas. Las capturas funcionales de entrega con Thunder Client son una actividad aparte.

## Monitoreo y DEMO

Conecta un monitor público a `ws://127.0.0.1:8000/ws/vehiculos/{id_vehiculo}`. Recibe primero `estado_inicial` y después eventos solo de ese vehículo; los mensajes del cliente no ejecutan acciones. Reconectar consulta nuevamente RDS. Cada cliente tiene cola de 64 eventos y un emisor con timeout de 2 segundos.

Los POST nuevos (sin cuerpo) son `/vehiculos/{id_vehiculo}/demos/{id_demo}/iniciar` (202 con UUID), `/vehiculos/{id_vehiculo}/demos/{id_demo}/cancelar` y `/vehiculos/{id_vehiculo}/detener`. La ejecución usa los N movimientos reales, confirma el estado antes de cada evento y nunca registra historial de DEMO. Durante una ejecución, los cambios incompatibles del mismo vehículo responden409; los GET y otros vehículos siguen disponibles.

Cancelar interrumpe esperas y espera cualquier operación RDS en curso antes de confirmar. DETENER es lógico: no inventa códigos ni afirma una parada física. `conectado` conserva su valor almacenado. Consulta [el protocolo, eventos, ejemplos y clases](docs/websocket.md).

Las carpetas separan responsabilidades: `models` define contratos; `repositories` accede a SP; `services` decide reglas, transacciones y tareas; `routes` adapta HTTP/WebSocket; `database` gestiona recursos; `config` y `security.py` leen configuración. `runtime.py` comparte el coordinador y los gestores. Los scripts están en `scripts`, las pruebas en `tests` y las explicaciones en `docs`.

## Examen 2.6: EC2 Windows, HTTPS y WSS

La [guía de operación en EC2 Windows](docs/operacion_ec2_windows.md) contiene actualización segura, instalación de la API con WinSW como `LocalService`, Caddy con binarios oficiales verificados, certificados persistentes y pruebas públicas. Los instaladores de [deploy/windows](deploy/windows/README.md) se ejecutan únicamente dentro de Windows Server, con `-InstallOnThisServer`; no cambian servicios, ACL ni firewall de la PC de desarrollo. Se mantiene un worker Uvicorn sin reload en loopback; Caddy publica HTTPS/WSS.

El hostname elegido es `184-72-72-255.sslip.io`, cuyo DNS se comprobó hacia la IP elástica. La configuración y los comandos están preparados: **eso no acredita que HTTPS/WSS, los servicios ni el certificado estén funcionando en EC2**. Los resultados realmente comprobados y los pendientes se detallan en la [matriz del examen](docs/examen_2_6.md) y la [validación de preparación](docs/verificacion_preparacion_2_6.json).

Los verificadores aceptan el destino, IDs reales y archivos nuevos; ya no hay que editar localhost ni asumir un historial de seis filas. Consultar `python -m scripts.verify_public --help`. Las escrituras requieren habilitación explícita, identifican cada operación y leen claves privadas sin imprimirlas. Conservar las evidencias 2.5; las nuevas se guardan en un directorio local excluido de Git y se revisan antes de publicarlas. Después de un reinicio, los clientes se reconectan al último estado persistido; no se reanudan sockets ni tareas DEMO en memoria.

La [guía Thunder Client](docs/thunder_client.md) y la colección HTTP preparada distinguen las tres pruebas exigidas de las verificaciones técnicas con Python. La [guía de los 21 SP](docs/guia_estudio_21_sp.md) sirve como base para los apuntes manuscritos. Las capturas, el Word editable y su PDF deben reflejar la ejecución real, sin inventar resultados.
