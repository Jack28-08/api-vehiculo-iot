# Guía de estudio: 21 procedimientos y sus operaciones HTTP

Fuente: contrato sanitizado versionado de la práctica 2.5, mapa de endpoints,
modelos Pydantic y servicios. Preparada para elaborar y comprender los apuntes
manuscritos; no sustituye las explicaciones propias del alumno. No vuelve a
inspeccionar ni modifica RDS.

Hay **21 SP, no 21 rutas de negocio distintas**: siete GET y trece POST cubren
los SP; actualizar estado y registrar historial comparten un POST transaccional.
Además hay tres POST de control de DEMO/detención y dos GET de salud. `/docs`
es la interfaz de documentación. El total OpenAPI es 25 operaciones GET/POST
(9 GET y 16 POST); el monitor WebSocket no se cuenta como una operación OpenAPI.

## Reglas que se repiten

- La ruta valida existencia del vehículo y pertenencia de DEMO, comando y paso.
  Un recurso inexistente o anidado ajeno responde `404`; una lista válida vacía
  responde `200` con `[]`. Estado sin fila previa es `200` con `null`.
- IDs: enteros positivos hasta 2 147 483 647. Nombres: vehículo/DEMO hasta 80,
  comando hasta 60; descripción hasta 255, modelo hasta 80 y microcontrolador
  hasta 50. `origen`: de 1 a 30 caracteres. Entradas adicionales se rechazan.
- `codigo` admite INT firmado, sin catálogo inventado. `orden > 0`,
  `duracion_ms > 0`, `pausa_ms >= 0`, todos hasta 2 147 483 647 por el parámetro
  **INT firmado del SP**, aunque una columna de tiempo sea UNSIGNED. Los
  booleanos HTTP son `true/false`; el contrato MySQL informa `tinyint(1)`.
- Actualización mediante POST es reemplazo completo: enviar obligatorios y los
  opcionales que se quieran conservar; los opcionales omitidos se vuelven NULL.
- GET y monitor: públicos. POST: `X-API-Key`; `401` si falta/es inválida y `403`
  si el rol no alcanza. `409`: duplicado, integridad, recurso inactivo al ejecutar
  o vehículo ocupado por DEMO; `422`: entrada inválida; `500`: fallo interno;
  `503`: RDS/configuración no disponible. Los errores no reflejan secretos.
- Repositorios permiten solo nombres internos de SP y valores enlazados; consumen
  todos los conjuntos de resultados. El servicio decide commit/rollback. Cada
  operación obtiene su conexión; el MySQL síncrono se ejecuta fuera del bucle
  asíncrono. La notificación WebSocket se publica después del commit.

## Contratos de respuesta

Los nombres de modelos siguientes se encuentran en `app/models/vehiculos.py`.
`VehiculoRespuesta`, `ComandoRespuesta` y `DemoRespuesta` contienen las columnas
de sus tablas. `EstadoRespuesta` tiene vehículo y comando del JOIN, sin
`id_estado`; `HistorialRespuesta` tiene nombre/código del comando, sin
`id_comando`; `DemoMovimientoRespuesta` tiene nombre/código y tiempos, sin
`id_demo`. No añadir columnas de tabla que el SELECT real no devuelve.
`EliminacionRespuesta` es `{"eliminado": true, "id": ...}`. No se entrega al
cliente HTTP el texto de `CALL`, el objeto conexión ni los valores privados.

## 21 fichas para los apuntes

### 1. `sp_obtener_vehiculos`

**Propósito:** Listar vehículos por identificador.

**Ruta y permiso:** `GET /vehiculos`; Público.

**Entrada HTTP:** Sin parámetros de ruta ni cuerpo.

**Parámetros SP, en su orden real:** Sin parámetros.

**Respuesta de la API:** 200: lista de VehiculoRespuesta; [] cuando no hay vehículos.

**Efecto y decisión:** Solo lee; no ejecuta comandos.

### 2. `sp_obtener_vehiculo`

**Propósito:** Leer un vehículo existente.

**Ruta y permiso:** `GET /vehiculos/{id_vehiculo}`; Público.

**Entrada HTTP:** id_vehiculo de la ruta.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`

**Respuesta de la API:** 200: VehiculoRespuesta; 404 si no existe.

**Efecto y decisión:** Solo lee.

### 3. `sp_obtener_comandos_vehiculo`

**Propósito:** Consultar el catálogo del vehículo, ordenado por codigo.

**Ruta y permiso:** `GET /vehiculos/{id_vehiculo}/comandos`; Público.

**Entrada HTTP:** id_vehiculo de la ruta.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`

**Respuesta de la API:** 200: lista de ComandoRespuesta; [] si el catálogo válido está vacío.

**Efecto y decisión:** Solo lee; listar o crear un comando no equivale a ejecutarlo.

### 4. `sp_obtener_estado`

**Propósito:** Consultar el JOIN de estado, vehículo y comando actual.

**Ruta y permiso:** `GET /vehiculos/{id_vehiculo}/estado`; Público.

**Entrada HTTP:** id_vehiculo de la ruta.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`

**Respuesta de la API:** 200: EstadoRespuesta o null si el vehículo existe sin estado previo.

**Efecto y decisión:** Solo lee. No inventa id_estado; el nombre/código del comando pueden ser null.

### 5. `sp_obtener_historial`

**Propósito:** Consultar historial por fecha_hora DESC y, en empates, id_historial DESC.

**Ruta y permiso:** `GET /vehiculos/{id_vehiculo}/historial`; Público.

**Entrada HTTP:** id_vehiculo de la ruta.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`

**Respuesta de la API:** 200: lista de HistorialRespuesta de cero a diez filas.

**Efecto y decisión:** El SP no tiene LIMIT: la API consume todos los resultados y selecciona los primeros diez; no borra RDS. El JOIN no devuelve id_comando.

### 6. `sp_obtener_demos`

**Propósito:** Consultar las DEMO del vehículo por id_demo.

**Ruta y permiso:** `GET /vehiculos/{id_vehiculo}/demos`; Público.

**Entrada HTTP:** id_vehiculo de la ruta.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`

**Respuesta de la API:** 200: lista de DemoRespuesta o [].

**Efecto y decisión:** Solo lee; no ejecuta las secuencias.

### 7. `sp_obtener_movimientos_demo`

**Propósito:** Leer los pasos reales de una DEMO ordenados por orden.

**Ruta y permiso:** `GET /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos`; Público.

**Entrada HTTP:** id_vehiculo e id_demo de la ruta; el servicio comprueba pertenencia.

**Parámetros SP, en su orden real:** 1. `p_id_demo` IN `int`

**Respuesta de la API:** 200: lista de DemoMovimientoRespuesta o []; 404 si la DEMO es ajena.

**Efecto y decisión:** El JOIN devuelve comando y codigo, pero no id_demo. El número de pasos N no es fijo.

### 8. `sp_crear_vehiculo`

**Propósito:** Crear un vehículo.

**Ruta y permiso:** `POST /vehiculos`; Administrador.

**Entrada HTTP:** Cuerpo VehiculoCrear: nombre; modelo, descripcion y microcontrolador opcionales.

**Parámetros SP, en su orden real:** 1. `p_nombre` IN `varchar(80)`; 2. `p_modelo` IN `varchar(80)`; 3. `p_descripcion` IN `varchar(255)`; 4. `p_microcontrolador` IN `varchar(50)`

**Respuesta de la API:** 201: VehiculoRespuesta confirmado.

**Efecto y decisión:** Inserta vehículo con activo inicial del SP; el SP devuelve LAST_INSERT_ID y el servicio vuelve a leer. No añade historial de movimientos.

### 9. `sp_actualizar_vehiculo`

**Propósito:** Reemplazar los campos editables del vehículo.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/actualizar`; Administrador.

**Entrada HTTP:** Ruta id_vehiculo; cuerpo VehiculoActualizar: nombre, activo y campos opcionales.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`; 2. `p_nombre` IN `varchar(80)`; 3. `p_modelo` IN `varchar(80)`; 4. `p_descripcion` IN `varchar(255)`; 5. `p_microcontrolador` IN `varchar(50)`; 6. `p_activo` IN `tinyint(1)`

**Respuesta de la API:** 200: VehiculoRespuesta confirmado; 404 si no existe.

**Efecto y decisión:** Actualiza vehículo; un opcional omitido se guarda como NULL. No cambia ID ni fecha_registro.

### 10. `sp_eliminar_vehiculo`

**Propósito:** Eliminar el vehículo comprobado.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/eliminar`; Administrador.

**Entrada HTTP:** id_vehiculo de la ruta; sin cuerpo.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`

**Respuesta de la API:** 200: EliminacionRespuesta con eliminado=true e id.

**Efecto y decisión:** Es eliminación real: RDS aplica sus relaciones/cascadas; 409 si hay conflicto de integridad. No usar para probar la entrega sin intención de borrar.

### 11. `sp_crear_comando`

**Propósito:** Crear un comando del catálogo del vehículo.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/comandos`; Administrador.

**Entrada HTTP:** Ruta id_vehiculo; cuerpo ComandoCrear: nombre, codigo y descripcion opcional.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`; 2. `p_nombre` IN `varchar(60)`; 3. `p_codigo` IN `int`; 4. `p_descripcion` IN `varchar(255)`

**Respuesta de la API:** 201: ComandoRespuesta confirmado.

**Efecto y decisión:** Inserta catálogo; no actualiza estado ni historial. Los duplicados protegidos por UNIQUE responden 409.

### 12. `sp_actualizar_comando`

**Propósito:** Reemplazar un comando del vehículo.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/comandos/{id_comando}/actualizar`; Administrador.

**Entrada HTTP:** Ruta id_vehiculo e id_comando; cuerpo ComandoActualizar: nombre, codigo, activo, descripcion opcional.

**Parámetros SP, en su orden real:** 1. `p_id_comando` IN `int`; 2. `p_nombre` IN `varchar(60)`; 3. `p_codigo` IN `int`; 4. `p_descripcion` IN `varchar(255)`; 5. `p_activo` IN `tinyint(1)`

**Respuesta de la API:** 200: ComandoRespuesta confirmado.

**Efecto y decisión:** Actualiza catálogo. El servicio valida pertenencia antes de llamar al SP que recibe solo el ID del comando.

### 13. `sp_eliminar_comando`

**Propósito:** Eliminar un comando del catálogo tras comprobar pertenencia.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/comandos/{id_comando}/eliminar`; Administrador.

**Entrada HTTP:** id_vehiculo e id_comando de la ruta; sin cuerpo.

**Parámetros SP, en su orden real:** 1. `p_id_comando` IN `int`

**Respuesta de la API:** 200: EliminacionRespuesta; 409 si está protegido por movimientos DEMO.

**Efecto y decisión:** RDS aplica RESTRICT a referencias de pasos DEMO y SET NULL en estado/historial; no es la ejecución de un comando de parada.

### 14. `sp_crear_demo`

**Propósito:** Crear la cabecera de una DEMO del vehículo.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/demos`; Administrador.

**Entrada HTTP:** Ruta id_vehiculo; cuerpo DemoCrear: nombre y descripcion opcional.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`; 2. `p_nombre` IN `varchar(80)`; 3. `p_descripcion` IN `varchar(255)`

**Respuesta de la API:** 201: DemoRespuesta confirmado.

**Efecto y decisión:** Inserta DEMO con activo inicial del SP; no crea pasos ni inicia ejecución.

### 15. `sp_actualizar_demo`

**Propósito:** Reemplazar una DEMO perteneciente al vehículo.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/actualizar`; Administrador.

**Entrada HTTP:** Ruta id_vehiculo e id_demo; cuerpo DemoActualizar: nombre, activo y descripcion opcional.

**Parámetros SP, en su orden real:** 1. `p_id_demo` IN `int`; 2. `p_nombre` IN `varchar(80)`; 3. `p_descripcion` IN `varchar(255)`; 4. `p_activo` IN `tinyint(1)`

**Respuesta de la API:** 200: DemoRespuesta confirmado.

**Efecto y decisión:** Actualiza cabecera, no ejecuta ni reordena pasos. Descripcion omitida se escribe NULL.

### 16. `sp_eliminar_demo`

**Propósito:** Eliminar una DEMO del vehículo.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/eliminar`; Administrador.

**Entrada HTTP:** id_vehiculo e id_demo de la ruta; sin cuerpo.

**Parámetros SP, en su orden real:** 1. `p_id_demo` IN `int`

**Respuesta de la API:** 200: EliminacionRespuesta.

**Efecto y decisión:** Elimina cabecera y movimientos mediante la relación CASCADE del contrato. No es cancelar una tarea activa.

### 17. `sp_agregar_movimiento_demo`

**Propósito:** Añadir un paso a una DEMO.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos`; Administrador.

**Entrada HTTP:** Ruta id_vehiculo e id_demo; cuerpo DemoMovimientoCrear: id_comando, orden, duracion_ms y pausa_ms (por defecto 0).

**Parámetros SP, en su orden real:** 1. `p_id_demo` IN `int`; 2. `p_id_comando` IN `int`; 3. `p_orden` IN `int`; 4. `p_duracion_ms` IN `int`; 5. `p_pausa_ms` IN `int`

**Respuesta de la API:** 201: DemoMovimientoRespuesta confirmado mediante lectura posterior.

**Efecto y decisión:** Valida comando del vehículo dueño de la DEMO, orden positivo, tiempos y UNIQUE (id_demo, orden). No ejecuta ni registra historial.

### 18. `sp_actualizar_movimiento_demo`

**Propósito:** Reemplazar comando, orden y tiempos de un paso existente.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos/{id_demo_movimiento}/actualizar`; Administrador.

**Entrada HTTP:** Ruta completa de vehículo, DEMO y movimiento; cuerpo DemoMovimientoActualizar.

**Parámetros SP, en su orden real:** 1. `p_id_demo_movimiento` IN `int`; 2. `p_id_comando` IN `int`; 3. `p_orden` IN `int`; 4. `p_duracion_ms` IN `int`; 5. `p_pausa_ms` IN `int`

**Respuesta de la API:** 200: DemoMovimientoRespuesta confirmado.

**Efecto y decisión:** Comprueba pertenencia de todos los recursos y del nuevo comando; 409 si se duplica el orden. No es ejecución física.

### 19. `sp_eliminar_movimiento_demo`

**Propósito:** Eliminar un paso perteneciente a esa DEMO.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos/{id_demo_movimiento}/eliminar`; Administrador.

**Entrada HTTP:** Ruta completa; sin cuerpo.

**Parámetros SP, en su orden real:** 1. `p_id_demo_movimiento` IN `int`

**Respuesta de la API:** 200: EliminacionRespuesta.

**Efecto y decisión:** Borra ese paso; no borra la DEMO ni añade historial. Puede quedar un hueco en los valores de orden.

### 20. `sp_actualizar_estado`

**Propósito:** Aplicar al estado un comando existente validado.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/movimientos`; Operador o administrador.

**Entrada HTTP:** Ruta id_vehiculo; cuerpo MovimientoEjecutar: id_comando y origen. p_conectado se obtiene del estado almacenado, o false si no existe; el cliente no lo envía.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`; 2. `p_id_comando` IN `int`; 3. `p_conectado` IN `tinyint(1)`

**Respuesta de la API:** 200: MovimientoConfirmado con estado e historial tras commit conjunto.

**Efecto y decisión:** INSERT ... ON DUPLICATE KEY UPDATE de estado. Comparte conexión/transacción con sp_registrar_historial. DEMO también reutiliza este SP por paso, pero sin registrar historial.

### 21. `sp_registrar_historial`

**Propósito:** Registrar el movimiento individual confirmado.

**Ruta y permiso:** `POST /vehiculos/{id_vehiculo}/movimientos`; Operador o administrador.

**Entrada HTTP:** Mismos ruta y cuerpo del movimiento individual; p_origen procede de origen.

**Parámetros SP, en su orden real:** 1. `p_id_vehiculo` IN `int`; 2. `p_id_comando` IN `int`; 3. `p_origen` IN `varchar(30)`

**Respuesta de la API:** 200: el mismo MovimientoConfirmado del SP anterior; no hay endpoint separado.

**Efecto y decisión:** Inserta historial en la misma transacción que el estado. Si falla este segundo SP se revierten ambos; el repositorio no hace commit separado. DEMO nunca lo utiliza.

## Controles que reutilizan SP y no agregan procedimientos

`POST /vehiculos/{id_vehiculo}/demos/{id_demo}/iniciar` valida y reserva el
vehículo, lee el plan real y responde `202` con UUID y N. Por cada paso llama
`sp_actualizar_estado`, confirma y publica `demo_movimiento`; espera duración y
pausa, incluida la del último, y publica `demo_fin`. No llama
`sp_registrar_historial`. Un segundo inicio del mismo vehículo responde `409`.

`POST .../cancelar` solicita cancelación y espera cualquier operación RDS en
curso antes de confirmar. `POST /vehiculos/{id_vehiculo}/detener` cancela y
notifica detención lógica: no inventa un comando físico de parada ni cambia
`conectado`. Los tres POST admiten operador o administrador y no agregan SP.

El monitor público `WSS /ws/vehiculos/{id_vehiculo}` envía primero
`estado_inicial`; después publica eventos del vehículo. Dos clientes simultáneos
deben observar el mismo evento confirmado. `id_ejecucion` relaciona inicio,
pasos y fin/cancelación/error. Abrir HTTP o WSS no demuestra conexión de hardware.
Tras reiniciar el proceso se reconecta al estado persistido: ni sockets ni DEMO
se reanudan desde memoria. Solo un worker, sin reload.

## Preguntas para explicarlo personalmente

1. ¿Por qué el movimiento individual necesita dos SP y un solo commit?
2. ¿Qué diferencia hay entre crear un comando y ejecutarlo, y entre crear una
   DEMO y aceptar su ejecución con 202?
3. ¿Por qué una DEMO no debe usar el SP de historial? ¿Cómo demostrarlo si el
   GET devuelve solo diez filas?
4. ¿Cómo se impide usar un comando de otro vehículo y qué ocurre al pedir un
   recurso inexistente frente a una lista vacía?
5. ¿Qué prueba un evento emitido después del commit? ¿Qué no prueba respecto a
   motores, conectado y entrega física?
6. ¿Qué se pierde al reiniciar EC2 y qué se conserva en RDS?
7. ¿Qué función cumple HTTPS/WSS en Caddy y por qué Uvicorn queda en loopback?

Referencias internas: [mapa SP](mapa_endpoints_sp.md),
[arquitectura](arquitectura_http.md), [protocolo](websocket.md) y
[matriz del examen](examen_2_6.md).
