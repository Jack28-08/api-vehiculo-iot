# Thunder Client: tres pruebas y dos monitores WSS

## Revisión real de la extensión y licencia

El 20 de octubre de 2026 se revisaron, en modo de solo lectura, las carpetas de
las versiones **2.41.7** y **3.0.0** instaladas. El registro local de extensiones
de VS Code selecciona **3.0.0**. Esto verifica archivos instalados, no la sesión
de cuenta, el plan activado ni una conexión pública desde la interfaz.

El antecedente 2.5 fue un bloqueo de WebSocket por licencia y los eventos se
comprobaron con Python. La versión 3.0.0, publicada el 17 de octubre de 2026,
anuncia WebSocket en el plan gratuito y exige iniciar sesión con una cuenta
gratuita, sin tarjeta. La [página oficial de planes](https://www.thunderclient.com/pricing)
y las [notas oficiales de 3.0.0](https://github.com/rangav/thunder-client-support/releases/tag/v3.0.0)
respaldan ese cambio. El plan gratuito publica un límite de tres colecciones,
treinta peticiones por colección y cinco importaciones mensuales.

Hay una discrepancia: la [página de importación](https://docs.thunderclient.com/features/import)
aún describe importación como función de pago, mientras las notas 3.0.0 y los
planes actuales incluyen importaciones limitadas. Confirmar el permiso real en
la UI actual. No se compró licencia, no se inició sesión por el alumno y no se
intentó evadir una restricción. Si la UI sigue bloqueando la función, conservar
esa evidencia y pedir al docente aceptación explícita de Python como alternativa;
no marcar automáticamente cumplido el requisito Thunder.

## Preparación de las peticiones HTTP

El hostname elegido es `184-72-72-255.sslip.io`. Las direcciones objetivo son
`https://184-72-72-255.sslip.io/docs` y
`wss://184-72-72-255.sslip.io/ws/vehiculos/ID_REAL`; el certificado y el acceso
HTTPS/WSS público todavía deben instalarse y comprobarse en EC2. Una resolución
DNS correcta no demuestra que esas URLs ya funcionen.

La colección [examen-2-6.postman_collection.json](../thunder-tests/examen-2-6.postman_collection.json)
usa el formato Postman 2.1.0 que Thunder documenta como importable. Contiene
peticiones HTTP, sin scripts ni tokens incrustados; no contiene conexiones WSS.
Su JSON se valida localmente, pero no se ha importado ni ejecutado desde la UI
de Thunder en esta preparación.

1. Abrir Thunder Client 3.0.0 en VS Code y comprobar/iniciar sesión gratuita
   desde su UI, sin compartir credenciales. Consultar el plan antes de importar.
2. En **Collections → menú → Import**, elegir el archivo. Si esa opción no está
   disponible, crear las peticiones manualmente con las rutas de esta guía;
   no modificar la extensión ni usar una versión antigua para sortear permisos.
3. Preparar un entorno **local privado, fuera del repositorio** con `baseUrl`
   igual a `https://184-72-72-255.sslip.io`, sin barra final. No aceptar certificados
   inválidos ni desactivar la comprobación TLS.
4. Consultar primero `/vehiculos`, los comandos y las DEMO para establecer
   `idVehiculo`, `idComando`, `idDemo`; `idOtroVehiculo` debe ser otro existente.
   Las variables publicadas están vacías para no asumir IDs antiguos.
5. Guardar la clave de operador en `apiKeyOperator` solo en el entorno local
   privado. Las claves proceden del `.env` privado del proyecto o del entorno;
   no exportar ese entorno, no escribirlas en la colección y ocultar el
   encabezado `X-API-Key` en capturas. Los GET y WSS no requieren clave.

**No usar “Run All”.** La colección es una plantilla de acciones manuales; un
POST individual añade una fila real y una DEMO modifica el estado. Un segundo
clic puede iniciar otra ejecución. Ejecutar cada POST solo en el destino propio
verificado, con intención de realizar esa operación y guardando su resultado.

## Dos conexiones WSS del mismo vehículo

Crear una petición de tipo **WebSocket**, seleccionar una URL
`wss://HOSTNAME_REAL/ws/vehiculos/ID_REAL` y conectar. Abrir una segunda petición
WebSocket independiente a la misma URL y conectarla; si la UI reutiliza una
pestaña, abrir otra ventana de VS Code. Deben quedar dos conexiones vivas,
identificadas como A y B, no dos capturas consecutivas de un solo socket.

Registrar `estado_inicial` de cada cliente y contrastarlo con GET `/estado` y
RDS. El JSON usa `tipo`, `id_vehiculo`, `id_comando`, `comando`, `codigo`,
`conectado` y `fecha_hora` UTC, más campos según evento. No enviar mensajes por
el monitor para intentar ejecutar acciones: los controles se realizan por POST.
Consultar [todos los campos y ejemplos](websocket.md).

Para aislamiento abrir C a `/ws/vehiculos/ID_OTRO_VEHICULO`. Después de su inicial
no debe recibir los movimientos de A/B. Al reconectar A debe recibir un nuevo
inicial con el estado actual; no prometer recuperar la secuencia de mensajes
que se perdió durante la desconexión.

## Prueba 1: movimiento individual transaccional

Con A y B conectados, elegir un comando activo del vehículo activo. Enviar
**una vez** `POST /vehiculos/{{idVehiculo}}/movimientos`, con el encabezado de
operador y cuerpo:

```json
{"id_comando": 123, "origen": "examen-2.6-thunder"}
```

`123` es ilustrativo: la colección utiliza `{{idComando}}` y hay que seleccionar
el ID real por GET. Esperar HTTP `200` con estado e historial confirmados y
`movimiento_individual` en A y B. Conservar el valor previo de `conectado`.
Leer estado e historial desde una conexión nueva a RDS para demostrar
persistencia; el endpoint confirma `sp_actualizar_estado` y
`sp_registrar_historial` dentro de una misma transacción. Su atomicidad ante un
fallo del segundo SP se prueba aisladamente; no provocar fallos en RDS de la
entrega. Si hay un fallo de red ambiguo, consultar persistencia antes de repetir.

Capturar respuesta HTTP sin claves, ambos eventos y comparación con la fila
persistida. Para rechazo sin autenticación está preparada la petición del mismo
POST sin `X-API-Key`; debe devolver `401`, sin modificar la base. El operador
recibe `403` al intentar gestión reservada a administrador; no usar por error
una clave administradora para esa comprobación.

## Prueba 2: historial público

Enviar `GET /vehiculos/{{idVehiculo}}/historial` sin clave. Comprobar HTTP `200`,
de cero a diez elementos y orden por fecha descendente y, en empates, ID
descendente. El SP devuelve todas las filas ordenadas y la API selecciona las
diez primeras; no elimina las anteriores. El número diez es un máximo, no una
cantidad que deba completarse creando movimientos de prueba.

## Prueba 3: DEMO sin modificar el historial

Consultar `/demos/{{idDemo}}/movimientos`; guardar el N real, `orden`, comando,
código, `duracion_ms` y `pausa_ms`. Obtener desde RDS un snapshot completo del
historial del vehículo: todas sus columnas, `COUNT(*)` y último ID. A/B deben
seguir conectados y no debe haber otros movimientos individuales simultáneos.

Enviar **una vez** `POST /vehiculos/{{idVehiculo}}/demos/{{idDemo}}/iniciar`, sin
cuerpo y con clave de operador. Registrar HTTP `202`, UUID y N. A y B deben
recibir, para ese UUID, exactamente `demo_inicio`, N `demo_movimiento` y
`demo_fin`. Contrastar orden y todos los campos con el plan leído en RDS.
Comprobar duración más pausa antes del paso siguiente y también después del
último antes de `demo_fin`; el acceso a RDS/red añade tiempo a esos mínimos.
Un `202` por sí solo no prueba que la DEMO haya terminado.

Tras el fin, leer estado e historial con otra conexión a RDS. El estado debe
corresponder al último comando; `conectado` se conserva. Todas las filas del
historial deben ser idénticas al snapshot, incluido contenido y cantidad; no
basta comparar los diez resultados del GET. Guardar evidencia completa en un
archivo nuevo. El verificador Python preparado ayuda a contrastar los datos,
pero sus eventos no deben presentarse como capturas obtenidas en Thunder.

## Concurrencia y cancelación, en una ejecución separada

Durante una DEMO activa, un segundo inicio del mismo vehículo debe devolver
`409`, sin crear otra tarea. Para cancelación iniciar otra ejecución de forma
deliberada, registrar su UUID y usar el POST `/cancelar`. La respuesta espera
la operación RDS que estuviera en curso. Tras confirmarla no puede haber nuevos
pasos del UUID cancelado. Registrar mensajes y tiempos de respuesta para
distinguir un paso ya emitido antes de la confirmación de un paso posterior.

El POST `/detener` es una detención lógica, no una garantía de parar motores.
El reinicio de Windows/servicios corta sockets y tareas en memoria: reconectar
consulta el último estado guardado, sin reanudar la DEMO interrumpida.

## Qué falta para marcar Thunder cumplido

Faltan la comprobación real de la cuenta/UI, el HTTPS/WSS desplegado y las tres
pruebas públicas capturadas. La revisión local de versiones, esta colección y
los eventos Python anteriores no sustituyen esas evidencias. Documentar
cualquier restricción que siga mostrando la UI y la decisión del docente.
