# Mapa de endpoints HTTP y procedimientos almacenados

Fuente de verdad: `docs/contrato_rds.json` y sus definiciones reales. Hay **21 filas de SP**, cubiertas por las **20 operaciones HTTP originales: 7 GET y 13 POST**. Los dos SP del movimiento individual comparten endpoint y transacción. Las verificaciones de existencia y pertenencia pueden reutilizar SP de consulta además del SP principal indicado en cada fila. Se añaden tres POST de iniciar/cancelar DEMO y detener, documentados en `docs/websocket.md`; reutilizan estos procedimientos sin cambiar sus definiciones ni añadir SP.

| Procedimiento almacenado | Endpoint funcional | Permiso | Propósito |
| --- | --- | --- | --- |
| `sp_obtener_vehiculos` | `GET /vehiculos` | Público | Listar vehículos. |
| `sp_obtener_vehiculo` | `GET /vehiculos/{id_vehiculo}` | Público | Obtener un vehículo; `404` si no existe. |
| `sp_obtener_comandos_vehiculo` | `GET /vehiculos/{id_vehiculo}/comandos` | Público | Obtener el catálogo del vehículo por código. |
| `sp_obtener_estado` | `GET /vehiculos/{id_vehiculo}/estado` | Público | Obtener el estado con su JOIN; `null` si no hay estado previo. |
| `sp_obtener_historial` | `GET /vehiculos/{id_vehiculo}/historial` | Público | Devolver hasta diez filas en el orden descendente del SP. |
| `sp_obtener_demos` | `GET /vehiculos/{id_vehiculo}/demos` | Público | Obtener las DEMO del vehículo. |
| `sp_obtener_movimientos_demo` | `GET /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos` | Público | Obtener movimientos por `orden`, comprobando pertenencia. |
| `sp_crear_vehiculo` | `POST /vehiculos` | Administrador | Crear y devolver el vehículo confirmado; `201`. |
| `sp_actualizar_vehiculo` | `POST /vehiculos/{id_vehiculo}/actualizar` | Administrador | Reemplazar los campos editables, incluido `activo`. |
| `sp_eliminar_vehiculo` | `POST /vehiculos/{id_vehiculo}/eliminar` | Administrador | Eliminar respetando las relaciones del esquema. |
| `sp_crear_comando` | `POST /vehiculos/{id_vehiculo}/comandos` | Administrador | Crear un comando del catálogo; `201`; no lo ejecuta. |
| `sp_actualizar_comando` | `POST /vehiculos/{id_vehiculo}/comandos/{id_comando}/actualizar` | Administrador | Reemplazar un comando perteneciente al vehículo. |
| `sp_eliminar_comando` | `POST /vehiculos/{id_vehiculo}/comandos/{id_comando}/eliminar` | Administrador | Eliminar un comando; `409` ante referencias protegidas. |
| `sp_crear_demo` | `POST /vehiculos/{id_vehiculo}/demos` | Administrador | Crear una DEMO; `201`; no ejecuta su secuencia. |
| `sp_actualizar_demo` | `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/actualizar` | Administrador | Reemplazar una DEMO del vehículo. |
| `sp_eliminar_demo` | `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/eliminar` | Administrador | Eliminar una DEMO y aplicar su cascada de movimientos. |
| `sp_agregar_movimiento_demo` | `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos` | Administrador | Añadir comando, orden y tiempos a una DEMO; `201`. |
| `sp_actualizar_movimiento_demo` | `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos/{id_demo_movimiento}/actualizar` | Administrador | Reemplazar un movimiento, validando DEMO y comando del vehículo. |
| `sp_eliminar_movimiento_demo` | `POST /vehiculos/{id_vehiculo}/demos/{id_demo}/movimientos/{id_demo_movimiento}/eliminar` | Administrador | Eliminar un movimiento perteneciente a la DEMO. |
| `sp_actualizar_estado` | `POST /vehiculos/{id_vehiculo}/movimientos` | Operador o administrador | Cambiar el comando actual, conservando el valor real de `conectado`. |
| `sp_registrar_historial` | `POST /vehiculos/{id_vehiculo}/movimientos` | Operador o administrador | Registrar el mismo movimiento en la transacción del cambio de estado. |

Los POST usan `X-API-Key`, integrado en **Authorize** de Swagger: credenciales ausentes o inválidas producen `401`; un operador que intenta gestionar recursos recibe `403`. Un recurso anidado ajeno al vehículo produce `404`. Identificadores, campos o tiempos inválidos producen `422`; duplicados, recursos inactivos al ejecutar movimientos y conflictos de integridad producen `409`. Los fallos internos producen mensajes fijos `500`, y la indisponibilidad de RDS o de la configuración de autenticación produce `503`.

Las rutas auxiliares `GET /health`, `GET /health/db` y `/docs` no corresponden a SP de negocio. `health/db` utiliza `SELECT 1`. No se agregan, eliminan ni redefinen procedimientos.
