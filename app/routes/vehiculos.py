"""Adaptadores HTTP síncronos: FastAPI los ejecuta fuera del bucle asíncrono.

Las rutas validan el contrato HTTP y delegan las reglas y transacciones en el
servicio. Los permisos se comprueban antes de acceder a la base de datos.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from app.models.vehiculos import (
    INT_MAX,
    ComandoActualizar,
    ComandoCrear,
    ComandoRespuesta,
    DemoActualizar,
    DemoCrear,
    DemoMovimientoActualizar,
    DemoMovimientoCrear,
    DemoMovimientoRespuesta,
    DemoRespuesta,
    EliminacionRespuesta,
    EstadoRespuesta,
    HistorialRespuesta,
    MovimientoConfirmado,
    MovimientoEjecutar,
    VehiculoActualizar,
    VehiculoCrear,
    VehiculoRespuesta,
)
from app.security import require_admin, require_operator
from app.services.vehiculos import VehiculoService
from app.runtime import CoordinatedVehiculoService, runtime


READ_RESPONSES = {
    404: {"description": "Vehículo o recurso anidado inexistente o ajeno al vehículo."},
    422: {"description": "Identificadores fuera del intervalo positivo de INT."},
    500: {"description": "Error interno; respuesta sin detalles sensibles."},
    503: {"description": "Amazon RDS no disponible."},
}
WRITE_RESPONSES = {
    **READ_RESPONSES,
    401: {"description": "Falta la clave de API o es inválida."},
    403: {"description": "La clave no tiene el permiso requerido."},
    409: {"description": "Duplicado, recurso inactivo o conflicto de integridad/relaciones."},
    422: {"description": "Cuerpo o identificadores inválidos para el contrato de los SP."},
}
router = APIRouter(prefix="/vehiculos", tags=["Vehículos"], responses=READ_RESPONSES)
IdRuta = Annotated[int, Path(ge=1, le=INT_MAX, description="Identificador positivo INT de MySQL.")]


def get_service() -> VehiculoService:
    """Una instancia ligera por solicitud; las conexiones son independientes."""
    return CoordinatedVehiculoService(runtime)


Service = Annotated[VehiculoService, Depends(get_service)]


@router.get("", response_model=list[VehiculoRespuesta], summary="Listar vehículos",
            description="Consulta sp_obtener_vehiculos. Devuelve los vehículos por id_vehiculo; una base vacía produce [].")
def listar_vehiculos(service: Service):
    return service.listar_vehiculos()


@router.get("/{id_vehiculo}", response_model=VehiculoRespuesta, summary="Obtener un vehículo",
            description="Consulta sp_obtener_vehiculo con el id de la ruta. Devuelve 404 si no existe.")
def obtener_vehiculo(id_vehiculo: IdRuta, service: Service):
    return service.obtener_vehiculo(id_vehiculo)


@router.get("/{id_vehiculo}/comandos", response_model=list[ComandoRespuesta], summary="Obtener comandos del vehículo",
            description="Valida el vehículo y consulta sp_obtener_comandos_vehiculo. Orden por codigo; [] es un catálogo vacío válido.")
def obtener_comandos(id_vehiculo: IdRuta, service: Service):
    return service.obtener_comandos(id_vehiculo)


@router.get("/{id_vehiculo}/estado", response_model=EstadoRespuesta | None, summary="Obtener el estado del vehículo",
            description="Valida el vehículo y consulta sp_obtener_estado. Devuelve el JOIN real o null si el vehículo todavía no tiene estado.")
def obtener_estado(id_vehiculo: IdRuta, service: Service):
    return service.obtener_estado(id_vehiculo)


@router.get("/{id_vehiculo}/historial", response_model=list[HistorialRespuesta], summary="Obtener los últimos diez registros",
            description="Valida el vehículo y consulta sp_obtener_historial. La API conserva solo los primeros diez del orden fecha_hora DESC, id_historial DESC; no elimina registros en RDS. Si hay menos, devuelve los disponibles.")
def obtener_historial(id_vehiculo: IdRuta, service: Service):
    return service.obtener_historial(id_vehiculo)


@router.get("/{id_vehiculo}/demos", response_model=list[DemoRespuesta], summary="Obtener DEMO del vehículo",
            description="Valida el vehículo y consulta sp_obtener_demos por id_vehiculo. Devuelve [] cuando todavía no existen DEMO.")
def obtener_demos(id_vehiculo: IdRuta, service: Service):
    return service.obtener_demos(id_vehiculo)


@router.get("/{id_vehiculo}/demos/{id_demo}/movimientos", response_model=list[DemoMovimientoRespuesta], summary="Obtener movimientos ordenados de una DEMO",
            description="Valida el vehículo y la pertenencia de id_demo; consulta sp_obtener_movimientos_demo. Devuelve el JOIN de movimientos y comandos ordenado por orden. Una DEMO ajena devuelve 404.")
def obtener_movimientos_demo(id_vehiculo: IdRuta, id_demo: IdRuta, service: Service):
    return service.obtener_movimientos_demo(id_vehiculo, id_demo)


@router.post("", response_model=VehiculoRespuesta, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Crear un vehículo", description="Administrador. sp_crear_vehiculo recibe nombre y los tres campos opcionales del cuerpo. El SP crea activo=true; devuelve el vehículo confirmado.")
def crear_vehiculo(payload: VehiculoCrear, service: Service):
    return service.crear_vehiculo(payload)


@router.post("/{id_vehiculo}/actualizar", response_model=VehiculoRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Actualizar un vehículo", description="Administrador. sp_actualizar_vehiculo reemplaza los campos editables del vehículo indicado, incluido activo. Los campos opcionales omitidos se escriben como NULL. Devuelve los datos confirmados; 404 si no existe.")
def actualizar_vehiculo(id_vehiculo: IdRuta, payload: VehiculoActualizar, service: Service):
    return service.actualizar_vehiculo(id_vehiculo, payload)


@router.post("/{id_vehiculo}/eliminar", response_model=EliminacionRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Eliminar un vehículo", description="Administrador. Valida existencia y llama sp_eliminar_vehiculo. RDS aplica las relaciones CASCADE del contrato. Devuelve confirmación tras commit; 409 ante conflicto de integridad.")
def eliminar_vehiculo(id_vehiculo: IdRuta, service: Service):
    return service.eliminar_vehiculo(id_vehiculo)


@router.post("/{id_vehiculo}/comandos", response_model=ComandoRespuesta, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Crear un comando de catálogo", description="Administrador. Valida el vehículo y llama sp_crear_comando con nombre, codigo INT firmado y descripcion. No ejecuta el comando. Duplicados de nombre o código dentro del vehículo devuelven 409.")
def crear_comando(id_vehiculo: IdRuta, payload: ComandoCrear, service: Service):
    return service.crear_comando(id_vehiculo, payload)


@router.post("/{id_vehiculo}/comandos/{id_comando}/actualizar", response_model=ComandoRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Actualizar un comando", description="Administrador. Valida existencia y pertenencia al vehículo antes de sp_actualizar_comando. Reemplaza nombre, codigo, descripcion y activo; una descripcion omitida se escribe NULL.")
def actualizar_comando(id_vehiculo: IdRuta, id_comando: IdRuta, payload: ComandoActualizar, service: Service):
    return service.actualizar_comando(id_vehiculo, id_comando, payload)


@router.post("/{id_vehiculo}/comandos/{id_comando}/eliminar", response_model=EliminacionRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Eliminar un comando", description="Administrador. Valida pertenencia y llama sp_eliminar_comando. Un comando utilizado por movimientos de DEMO está protegido por RESTRICT y devuelve 409; RDS conserva historial/estado con su relación SET NULL.")
def eliminar_comando(id_vehiculo: IdRuta, id_comando: IdRuta, service: Service):
    return service.eliminar_comando(id_vehiculo, id_comando)


@router.post("/{id_vehiculo}/demos", response_model=DemoRespuesta, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Crear una DEMO", description="Administrador. Valida el vehículo y llama sp_crear_demo con nombre y descripcion opcional; el SP crea activo=true. Solo crea la secuencia, sin ejecución temporizada.")
def crear_demo(id_vehiculo: IdRuta, payload: DemoCrear, service: Service):
    return service.crear_demo(id_vehiculo, payload)


@router.post("/{id_vehiculo}/demos/{id_demo}/actualizar", response_model=DemoRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Actualizar una DEMO", description="Administrador. Valida pertenencia de id_demo y llama sp_actualizar_demo. Reemplaza nombre, descripcion y activo; una descripcion omitida se escribe NULL.")
def actualizar_demo(id_vehiculo: IdRuta, id_demo: IdRuta, payload: DemoActualizar, service: Service):
    return service.actualizar_demo(id_vehiculo, id_demo, payload)


@router.post("/{id_vehiculo}/demos/{id_demo}/eliminar", response_model=EliminacionRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Eliminar una DEMO", description="Administrador. Valida pertenencia y llama sp_eliminar_demo. La relación CASCADE elimina sus movimientos. Devuelve confirmación después del commit.")
def eliminar_demo(id_vehiculo: IdRuta, id_demo: IdRuta, service: Service):
    return service.eliminar_demo(id_vehiculo, id_demo)


@router.post("/{id_vehiculo}/demos/{id_demo}/movimientos", response_model=DemoMovimientoRespuesta,
             status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Agregar un movimiento a una DEMO", description="Administrador. sp_agregar_movimiento_demo recibe id_comando, orden positivo, duracion_ms positiva y pausa_ms no negativa; todos dentro de INT firmado. Valida que DEMO y comando pertenezcan al vehículo. Un orden duplicado devuelve 409.")
def crear_movimiento_demo(id_vehiculo: IdRuta, id_demo: IdRuta, payload: DemoMovimientoCrear, service: Service):
    return service.crear_movimiento_demo(id_vehiculo, id_demo, payload)


@router.post("/{id_vehiculo}/demos/{id_demo}/movimientos/{id_demo_movimiento}/actualizar", response_model=DemoMovimientoRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Actualizar un movimiento de DEMO", description="Administrador. Valida vehículo, DEMO, movimiento y pertenencia del comando antes de sp_actualizar_movimiento_demo. Reemplaza comando, orden y tiempos con los límites reales de los SP.")
def actualizar_movimiento_demo(id_vehiculo: IdRuta, id_demo: IdRuta, id_demo_movimiento: IdRuta,
                               payload: DemoMovimientoActualizar, service: Service):
    return service.actualizar_movimiento_demo(id_vehiculo, id_demo, id_demo_movimiento, payload)


@router.post("/{id_vehiculo}/demos/{id_demo}/movimientos/{id_demo_movimiento}/eliminar", response_model=EliminacionRespuesta,
             dependencies=[Depends(require_admin)], responses=WRITE_RESPONSES,
             summary="Eliminar un movimiento de DEMO", description="Administrador. Valida toda la pertenencia de la ruta y llama sp_eliminar_movimiento_demo. Devuelve confirmación después del commit; un recurso ajeno devuelve 404.")
def eliminar_movimiento_demo(id_vehiculo: IdRuta, id_demo: IdRuta, id_demo_movimiento: IdRuta, service: Service):
    return service.eliminar_movimiento_demo(id_vehiculo, id_demo, id_demo_movimiento)


@router.post("/{id_vehiculo}/movimientos", response_model=MovimientoConfirmado,
             dependencies=[Depends(require_operator)], responses=WRITE_RESPONSES,
             summary="Ejecutar un comando existente", description="Operador o administrador. Valida vehículo y comando activos y su pertenencia. Ejecuta sp_actualizar_estado y sp_registrar_historial en la misma conexión/transacción: confirma ambos o revierte ambos. Conserva conectado del estado previo, o false si no existe. Publica movimiento_individual por WebSocket solo después del commit, sin acreditar actuación física. Durante una DEMO responde409.")
def ejecutar_movimiento(id_vehiculo: IdRuta, payload: MovimientoEjecutar, service: Service):
    return service.ejecutar_movimiento(id_vehiculo, payload)
