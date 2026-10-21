# WebSocket y ejecución lógica de DEMO

Esta etapa transmite estado y operaciones confirmadas por RDS. **Recibir un evento no confirma ejecución física de motores.** El microcontrolador se integrará en otra práctica. El valor `conectado` procede del estado almacenado; abrir un monitor no lo cambia.

## Arranque y alcance

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Se utiliza un solo worker local. Clientes, tareas y coordinación viven en ese proceso: no se comparten entre varios workers o servidores. Evita `--reload` durante una ejecución: reiniciar el proceso interrumpe la tarea. El cierre normal solicita cancelación, espera los workers RDS, termina las ejecuciones y cierra los clientes.

## Conectar un monitor

URL: `ws://127.0.0.1:8000/ws/vehiculos/{id_vehiculo}`; por ejemplo, `ws://127.0.0.1:8000/ws/vehiculos/1` para un vehículo que exista.

El monitor es público, como los GET. No acepta acciones, claves ni órdenes por mensajes: los mensajes entrantes se ignoran. Las escrituras solo se autorizan por los POST HTTP con `X-API-Key`. Nunca se coloca una clave en la URL.

Cada conexión consulta RDS y recibe primero `estado_inicial`, seguido por los eventos posteriores de su vehículo. El snapshot, el registro del cliente, los commits y el encolado de eventos usan la misma coordinación por vehículo para evitar el hueco entre lectura y suscripción. Se permiten varios clientes simultáneos; no se mezclan vehículos.

Si el vehículo no existe, se acepta el handshake y se cierra con **4404**. Un identificador fuera del rango positivo de INT se rechaza antes del handshake (cierre ASGI1008, normalmente HTTP403 para el cliente). Si RDS no permite abrir el monitor se cierra con **1013**, sin detalles internos.

Cada cliente tiene una cola máxima de **64 eventos**, un solo emisor y un timeout de envío de **2 segundos**. Si se llena la cola o no se puede enviar, se desconecta ese cliente con 1013. Los demás continúan. Al reconectar se consulta el último estado nuevamente; no hay replay de eventos perdidos ni garantía de entrega durable.

## Formato JSON

Todos los eventos contienen `tipo`, `id_vehiculo`, `id_comando`, `comando`, `codigo` y `fecha_hora`. Incluyen además `conectado` y los metadatos de ejecución que correspondan; los campos no aplicables son `null`.

Las marcas temporales se serializan en UTC con `Z`. Las sesiones MySQL utilizan `time_zone='+00:00'`. En el estado inicial y en los movimientos, `fecha_hora` procede de la fecha del estado o del historial confirmado; si no existe estado se usa la hora del evento. Inicio y desenlaces de DEMO usan la hora UTC de publicación. El orden de recepción define la secuencia, sin reordenar por timestamps.

Ejemplo de vehículo sin estado previo:

```json
{
  "tipo": "estado_inicial",
  "id_vehiculo": 1,
  "id_comando": null,
  "comando": null,
  "codigo": null,
  "fecha_hora": "2026-10-16T05:00:00Z",
  "conectado": null,
  "id_ejecucion": null,
  "id_demo": null,
  "total_movimientos": null,
  "orden": null,
  "duracion_ms": null,
  "pausa_ms": null,
  "mensaje": null
}
```

Ejemplo ilustrativo de un paso de una DEMO con tres movimientos. Los identificadores, nombres y códigos reales se obtienen del catálogo de RDS:

```json
{
  "tipo": "demo_movimiento",
  "id_vehiculo": 1,
  "id_comando": 2,
  "comando": "Comando del catálogo",
  "codigo": 2,
  "fecha_hora": "2026-10-16T05:00:01Z",
  "conectado": false,
  "id_ejecucion": "ff60ba3a-76b9-4802-a591-64f11bf82c64",
  "id_demo": 1,
  "total_movimientos": 3,
  "orden": 1,
  "duracion_ms": 1000,
  "pausa_ms": 100,
  "mensaje": null
}
```

| Tipo | Significado |
| --- | --- |
| `estado_inicial` | Último estado disponible al suscribirse; no modifica RDS. |
| `movimiento_individual` | Estado e historial ya confirmados por el POST existente. |
| `demo_inicio` | Ejecución aceptada y reservada, con UUID, DEMO y cantidad real de movimientos. |
| `demo_movimiento` | Un paso cuyo estado se confirmó, con orden, comando, duración y pausa. |
| `demo_fin` | Terminaron los N pasos y sus esperas. |
| `demo_cancelada` | Se interrumpió la ejecución y terminaron sus operaciones en curso. |
| `demo_error` | La secuencia se detuvo por un error; `mensaje` es público y fijo. |
| `detencion_logica` | DETENER fue atendido por la API; no acredita una parada física. |

Cada ejecución tiene exactamente un desenlace: `demo_fin`, `demo_cancelada` o `demo_error`. `detencion_logica` es una operación de control distinta, no otro desenlace de DEMO.

## Operaciones HTTP

Todas utilizan la autorización existente, para **operador o administrador**, mediante `X-API-Key`. Swagger **Authorize** permite usar una clave del `.env` privado sin publicarla. Una clave ausente o inválida produce 401 y permisos insuficientes producen 403.

| Método y ruta | Respuesta y efecto |
| --- | --- |
| `POST /vehiculos/{id_vehiculo}/movimientos` | 200; ejecuta un comando existente y confirma estado e historial juntos. Publica `movimiento_individual` después del commit. |
| `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/iniciar` | 202; valida y reserva, devuelve `id_ejecucion`, `id_vehiculo`, `id_demo`, `total_movimientos` y `estado: iniciada`. Sin cuerpo. |
| `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/cancelar` | 200 después del desenlace; devuelve los identificadores y el estado final. 409 sin ejecución activa; 404 si la DEMO indicada no es la activa del vehículo. Sin cuerpo. |
| `POST /vehiculos/{id_vehiculo}/detener` | 200; cancela la DEMO activa si existe, espera operaciones anteriores y publica `detencion_logica`. Devuelve `detencion_logica: true`, estado e identificadores. Sin cuerpo. |

Iniciar valida vehículo y DEMO activos, pertenencia, comandos existentes y activos del mismo vehículo, orden positivo sin duplicados y tiempos válidos. Rechaza una DEMO vacía o inválida con 409; recursos inexistentes/ajenos con 404. Los identificadores HTTP inválidos producen 422; errores de conexión producen 503. Un error durante la tarea ya aceptada se informa mediante `demo_error`, no mediante una respuesta 202 retrospectivamente cambiada.

La secuencia se obtiene mediante `sp_obtener_movimientos_demo` y utiliza sus **N movimientos reales**, sin asumir ocho. Para cada paso se abre una transacción independiente, se vuelve a validar, se conserva el valor de `conectado`, se ejecuta `sp_actualizar_estado`, se confirma y se cierra. Después se publica el paso y se espera `duracion_ms`, seguida de `pausa_ms`, con esperas asíncronas interrumpibles. La pausa no envía un comando adicional. **DEMO nunca llama a `sp_registrar_historial` ni inserta en historial.**

Mientras está reservada una DEMO se rechazan con 409 los movimientos individuales y todas las actualizaciones, eliminaciones o creaciones anidadas incompatibles de ese vehículo. Crear otro vehículo, consultar por GET, monitorear, cancelar y operar sobre otros vehículos sigue permitido. Las validaciones y esperas MySQL se ejecutan fuera del bucle asíncrono.

## Cancelación y prioridad

La petición de cancelación despierta inmediatamente una espera. Si hay un SP en un hilo, **no se abandona ese hilo**: se espera a que termine y cierre su transacción. Un paso ya iniciado puede confirmarse y publicar `demo_movimiento` antes de `demo_cancelada`. La respuesta que confirma la cancelación llega después; desde ese momento no habrá nuevos pasos de **ese UUID**. Si la ejecución ya había terminado se devuelve su desenlace real, sin afirmar que se canceló una ejecución finalizada.

DETENER también impide nuevos inicios y escrituras incompatibles mientras está siendo atendido. No busca ni inventa un código DETENER, no cambia `conectado` y no simula una parada escribiendo otro comando. El último comando almacenado puede permanecer en el estado. Las notificaciones WebSocket fallidas posteriores a un commit no revierten, repiten ni convierten ese commit en una escritura fallida.

Los UUID separan ejecuciones: la limpieza de una tarea anterior no puede liberar la reserva de una nueva. El coordinador es local al proceso; no hay distribución entre workers ni reanudación durable después de un cierre forzado.

## Clases nuevas

| Clase | Responsabilidad |
| --- | --- |
| `VehicleEvent` | Contrato JSON y normalización UTC. |
| `ConnectionManager` / `ClientConnection` | Registro por vehículo, cola acotada, emisor serial, desconexión y cierre. |
| `VehicleCoordinator` | Mutex por vehículo, reserva por identidad y prioridad de DETENER. |
| `DemoPlan` / `DemoDataService` | Instantánea validada y transacciones MySQL de cada paso, sin historial. |
| `DemoExecution` / `DemoExecutionManager` | UUID, señales de cancelación, tareas retenidas, N pasos y único desenlace. |
| `CoordinatedVehiculoService` | Envuelve los casos HTTP existentes para aplicar el mismo mutex y notificar después del commit. |
| `AppRuntime` | Comparte estos componentes y administra su ciclo de vida. |

## Verificación

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# Con el servidor local iniciado, solo lecturas reales:
$marcaVerificacionIoT = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
.\.venv\Scripts\python.exe -m scripts.check_http_reads --base-url http://127.0.0.1:8000 --output ".\evidencias\get-$marcaVerificacionIoT.json"
.\.venv\Scripts\python.exe -m scripts.check_websocket_reads --base-url http://127.0.0.1:8000 --output ".\evidencias\ws-$marcaVerificacionIoT.json"
```

Las suites `test_realtime` y `test_realtime_routes` aíslan las escrituras: prueban dos monitores, aislamiento, orden inicial/commit, rollback sin éxito, clientes lentos, errores, reconexión, N variable, ausencia de historial, inicios concurrentes, cancelación durante duración/pausa/RDS, falta de pasos posteriores, prioridad de DETENER y limpieza al cerrar.

Los dos scripts de lectura anteriores solo ejecutan GET y snapshots del monitor: no inician DEMO ni envían movimientos. Sus evidencias están en `docs/verificacion_http_get.json` y `docs/verificacion_websocket_lectura.json`.

Posteriormente se verificó una ejecución real de DEMO con **dos clientes WebSocket de Python**, mediante `websockets`. La [evidencia completa](verificacion_demo_real_completa.json) contiene HTTP `202`, UUID, estado inicial y los diez eventos de la ejecución en cada cliente: inicio, ocho movimientos y fin. Los campos de cada paso coinciden con RDS y se respetan las esperas mínimas de duración más pausa, incluida la espera del último movimiento antes del fin. Los intervalos observados incluyen la latencia de RDS y red; no miden la actuación de motores. Todas las filas del historial anterior y posterior son idénticas: seis registros, último ID `6`. Esta ejecución únicamente actualizó el estado mediante la DEMO; no repitió el movimiento individual.

El [intento anterior](verificacion_demo_real.json) quedó incompleto por un `PermissionError` al guardar y se conserva como tal. El verificador corregido prueba la escritura antes del POST y acumula los eventos en memoria durante la escucha. Las capturas de entrega en Thunder Client son una actividad distinta de esta comprobación con clientes Python.

Referencias: [WebSockets en FastAPI](https://fastapi.tiangolo.com/advanced/websockets/), [ciclo de vida](https://fastapi.tiangolo.com/advanced/events/) y [tareas, shield y to_thread de Python](https://docs.python.org/3.14/library/asyncio-task.html).
