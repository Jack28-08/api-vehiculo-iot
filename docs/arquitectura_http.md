# Cómo funciona la capa HTTP

Esta etapa conecta los casos de uso HTTP con los **21 SP existentes**. Las seis tablas, sus datos y las definiciones del contrato se conservan. La organización permite estudiar una regla en el servicio, comprobarla con dobles de prueba y seguir su llamada hasta el SP real.

La [guía de POO](poo.md) detalla las entidades de dominio, la clase abstracta
`Movimiento`, sus dos implementaciones y los controladores que registran
métodos de instancia en FastAPI. Incluye una demostración local reproducible.

## Responsabilidad de cada clase y carpeta

| Ubicación y componente | Responsabilidad y decisión |
| --- | --- |
| `app/models/vehiculos.py`: modelos Pydantic | Separan entrada y respuesta. Por ejemplo, `MovimientoEjecutar` recibe `id_comando` y `origen`; `MovimientoConfirmado` representa estado e historial confirmados. Los modelos rechazan propiedades desconocidas y validan tipos, longitudes y límites antes de consultar MySQL. |
| `Procedure`, en `app/repositories/vehiculos.py` | Enum con los 21 nombres internos permitidos. El cliente HTTP nunca puede proporcionar un nombre de SP, tabla o columna. |
| `ProcedureExecutor` | Ejecuta `CALL` únicamente para miembros de `Procedure`, verifica la cantidad de parámetros y envía sus valores enlazados con `%s`. Recorre y consume todos los conjuntos de resultados, incluidos los vacíos y el conjunto final sin columnas. Nunca confirma ni revierte transacciones. |
| `VehiculoRepository` | Da nombres del dominio a las llamadas: `obtener_demos`, `actualizar_estado`, etc. Recibe la conexión del servicio y devuelve filas o identificadores. También encapsula las consultas auxiliares constantes de bloqueo y `LAST_INSERT_ID()`. No decide permisos ni hace `commit`. |
| `VehiculoService`, en `app/services/vehiculos.py` | Implementa cada caso de uso, comprueba existencia, pertenencia y actividad, construye la respuesta y decide qué operaciones comparten una transacción. Sus dependencias se pueden sustituir para probar escrituras sin RDS. |
| `Vehiculo`, `Comando` y `Demo`, en `app/domain/vehiculos.py` | Entidades inmutables con métodos que exigen actividad y pertenencia. Se construyen con las filas de RDS; sus reglas no dependen de FastAPI. |
| `Movimiento`, `MovimientoIndividual` y `MovimientoDeDemo`, en `app/services/movimientos.py` | El método plantilla comparte la escritura de estado y aplica por despacho polimórfico la política de historial de cada subclase. El servicio conserva la propiedad de la transacción y la notificación sigue después del commit. |
| `MySQLConnectionManager`, en `app/database/connection.py` | Abre conexiones independientes, aplica tiempos de espera y garantiza el intento de cierre de cursores y conexiones. `transaction()` inicia, confirma al salir correctamente y revierte ante un fallo. `readonly=True` pide a MySQL una transacción de lectura. |
| `Settings`, en `app/config/settings.py` | Lee las cinco variables `MYSQL_*` desde entorno o `.env`, sin abrir una conexión. La contraseña utiliza `SecretStr`; la configuración no forma parte de las respuestas. |
| `ApiKeySettings`, `Role` y dependencias de `app/security.py` | Cargan las claves secretas y distinguen operador y administrador. `require_operator` admite ambos roles; `require_admin` admite únicamente administrador. Las claves deben ser no vacías y distintas; una configuración inválida cierra el acceso con `503`. |
| `VehiculoController`, en `app/routes/vehiculos.py` | Instancia un router y registra sus métodos enlazados. Adapta parámetros y cuerpos HTTP a llamadas del servicio; declara modelos, errores, permisos y descripciones de Swagger. Los métodos usan `def`, por lo que FastAPI ejecuta el acceso síncrono en su grupo de hilos. |
| `ApplicationError`, en `app/errors.py` | Representa un rechazo funcional esperado mediante código HTTP y mensaje público fijo. El servicio no necesita importar FastAPI para indicar un `404` o `409`. |
| `app/main.py` | Compone routers y traduce errores funcionales, de validación y de MySQL a respuestas seguras. No refleja cuerpos inválidos ni mensajes completos del conector. |
| `app/websocket/` y `app/runtime.py` | Monitores por vehículo, coordinación compartida y notificación después del commit. La ejecución DEMO se explica en [el protocolo WebSocket](websocket.md). |

Una instancia de servicio o repositorio no equivale a una conexión compartida. Cada caso de uso obtiene una nueva sesión mediante el gestor. Esto impide que dos solicitudes concurrentes mezclen estado de transacción o `LAST_INSERT_ID()`.

## Qué significa respetar el contrato real

Los resultados se modelan a partir de los `SELECT` de los SP. `EstadoRespuesta` contiene los nombres de vehículo y comando aportados por el JOIN y no inventa `id_estado`. `HistorialRespuesta` contiene nombre y código del comando, pero no añade un `id_comando` que el SP no selecciona. `DemoMovimientoRespuesta` utiliza el JOIN real y no exige `id_demo`, que tampoco aparece en esa proyección. Las columnas opcionales admiten `null`, por ejemplo cuando se elimina un comando y actúa una relación `SET NULL`.

Antes de consultar un recurso anidado, el servicio busca el vehículo. Si no existe, responde `404`. Si existe y no tiene comandos, DEMO o historial, responde `200` con `[]`; si no tiene estado, responde `200` con `null`. Una DEMO debe estar entre las del vehículo; un movimiento debe estar entre los de esa DEMO; el comando utilizado debe pertenecer al mismo vehículo. Un identificador válido de otro propietario se responde como `404`.

`sp_obtener_historial` ya ordena por `fecha_hora DESC, id_historial DESC` y no contiene `LIMIT`. El ejecutor consume todas sus filas y conjuntos para dejar libre la conexión. Después, el servicio usa `rows[:10]` al construir la respuesta: devuelve de cero a diez registros y no elimina ni recorta el historial almacenado. Esta decisión implica transferir el historial completo desde RDS en esta etapa; una futura optimización requerirá revisar el contrato del SP.

## Validaciones de entrada y actualizaciones completas

Los identificadores de ruta y cuerpo son positivos y están limitados por `INT` firmado: máximo **2 147 483 647**. Los cuerpos requieren enteros reales para los campos enteros y booleanos reales para `activo`. `codigo` admite el intervalo completo de `INT` firmado, porque el contrato no define un catálogo cerrado de códigos. `origen` es texto no vacío de hasta 30 caracteres; los modelos no inventan una lista de orígenes.

Los nombres admiten hasta 80 caracteres en vehículos y DEMO, y 60 en comandos. `modelo` admite 80, `microcontrolador` 50 y las descripciones 255. Los nombres se recortan en los extremos y no pueden quedar vacíos. Las creaciones delegan en los SP el valor inicial `activo=true`.

En movimientos de DEMO, `orden` es positivo, `duracion_ms > 0` y `pausa_ms >= 0`; todos tienen máximo **2 147 483 647**. Aunque las columnas de tiempo son `INT UNSIGNED`, los parámetros reales de sus SP son `INT` firmado; por eso se usa el límite menor del parámetro. El par `(id_demo, orden)` es UNIQUE y un orden duplicado genera `409`. Antes de escribir se comprueba que el comando corresponda al vehículo propietario de la DEMO.

Las rutas `/actualizar` usan **POST con sustitución completa** de los campos editables, según la práctica y los SP existentes. `nombre`, `activo` y los demás campos obligatorios deben enviarse; los campos opcionales omitidos toman `None` y se escriben como `NULL`. No son actualizaciones parciales: para conservar una descripción o un modelo hay que enviarlos de nuevo.

## Movimiento individual: una transacción para dos SP

`POST /vehiculos/{id_vehiculo}/movimientos` ejecuta un comando del catálogo existente. La creación del catálogo tiene su propia ruta administrativa `POST /vehiculos/{id_vehiculo}/comandos`.

El recorrido de `VehiculoService.ejecutar_movimiento` es:

1. Abrir una conexión y una transacción, con `autocommit=False`.
2. Bloquear la fila del vehículo mediante un `SELECT ... FOR UPDATE`, comprobar existencia y exigir `activo=true`.
3. Bloquear el comando, comprobar que pertenece al vehículo y exigir `activo=true`.
4. Leer `conectado` del estado mediante otra lectura con bloqueo. Si no hay estado previo, tomar `false`. Una petición HTTP no acredita presencia del microcontrolador.
5. Ejecutar `sp_actualizar_estado` y consumir todos sus resultados.
6. Ejecutar `sp_registrar_historial` y consumir todos sus resultados, dentro de la misma conexión.
7. Consultar `LAST_INSERT_ID()` en esa sesión para identificar el historial recién insertado; recuperar los resultados reales y validarlos con Pydantic.
8. Salir del contexto para hacer **un solo commit**. Si falla el segundo SP, la construcción de la respuesta o el commit, el gestor intenta rollback y cierre; no se envía una respuesta de éxito.
9. Devolver el resultado una vez confirmado.

Las consultas auxiliares `SELECT ... FOR UPDATE` son lecturas con bloqueo y parámetros enlazados; ayudan a coordinar validaciones y cambios concurrentes sin redefinir los SP. El bloqueo del vehículo serializa las escrituras relacionadas que pasan por esta API. La lectura directa del estado bloqueado conserva su valor actual incluso si existe una sesión concurrente. Las reglas de integridad del propio esquema siguen siendo la última protección ante otros clientes de MySQL.

`LAST_INSERT_ID()` es propio de la conexión y no depende de qué registro tenga la fecha mayor. Se necesita porque `sp_registrar_historial` inserta, pero no devuelve el identificador. En `sp_agregar_movimiento_demo`, que tampoco devuelve una fila, la respuesta se recupera con el SP de consulta usando el par UNIQUE `(id_demo, orden)` ya validado.

`CoordinatedVehiculoService` publica `movimiento_individual` **después del commit** y antes de liberar el mutex del vehículo. Una notificación fallida no provoca repetir ni revertir la escritura. WebSocket transmite el estado confirmado de RDS a los monitores; la ejecución física por el microcontrolador se integrará posteriormente y no se presume por recibir eventos.

Si se pierde la conexión durante el commit, el cliente puede no saber si RDS llegó a confirmar. El gestor no reintenta escrituras automáticamente; resolver reintentos seguros requerirá un mecanismo de idempotencia posterior.

## Eliminaciones y errores

Se llama a los SP de eliminación existentes; la API no altera claves foráneas ni ejecuta borrados manuales adicionales. El contrato define `CASCADE` desde vehículo hacia comandos, DEMO, estado e historial, y desde DEMO hacia sus movimientos. La referencia de movimiento de DEMO a comando usa `RESTRICT`: un comando referenciado no se puede eliminar y el conflicto se traduce a `409`. Al eliminar un comando permitido, las referencias de estado e historial usan `SET NULL` y conservan esas filas. Cualquier conflicto que determine MySQL, también durante cascadas, se informa como `409`.

Las respuestas distinguen `401` por clave ausente o inválida, `403` por rol insuficiente, `404` por recurso inexistente o ajeno, `409` por duplicados/integridad o por ejecutar un recurso inactivo y `422` por entrada inválida. Los errores internos se traducen a `500`; los fallos de disponibilidad o configuración de conexión a `503`. Se publican mensajes fijos, sin contraseñas, nombres de servidor, claves ni SQL recibido del conector. Swagger presenta los cuerpos, respuestas y SP relacionados en cada operación.

## Cómo estudiar y verificar las decisiones

Sigue una ruta hasta su método de servicio y de ahí al repositorio y al enum `Procedure`. Compara el orden de los argumentos con `docs/contrato_rds.json`; después revisa el modelo de respuesta contra el `SELECT` del SP. Así puedes modificar una regla en la capa que le corresponde.

Las pruebas de autenticación, pertenencia, límites y rollback usan aplicaciones ASGI temporales, archivos temporales y dobles de gestor/repositorio. Permiten simular un fallo justo en el segundo SP y comprobar que no queda confirmado el primero sin modificar RDS. Las consultas reales de comprobación se limitan a GET y a las transacciones de lectura de la API. No se realizan escrituras de prueba ni cambios de esquema en la base existente.

WebSocket y ejecución temporizada de DEMO ya se describen en `docs/websocket.md`. Quedan para después la integración física del microcontrolador y las tres pruebas funcionales de entrega en Thunder Client.
