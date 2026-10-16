"""Control HTTP autorizado de tareas DEMO administradas por la aplicación."""

import asyncio

from fastapi import APIRouter, Depends

from app.models.demos import DemoExecutionResponse, LogicalStopResponse
from app.routes.vehiculos import IdRuta, WRITE_RESPONSES
from app.runtime import runtime
from app.security import require_operator

router = APIRouter(prefix="/vehiculos", tags=["Ejecución DEMO"],
                   dependencies=[Depends(require_operator)], responses=WRITE_RESPONSES)


@router.post("/{id_vehiculo}/demos/{id_demo}/iniciar", status_code=202,
             response_model=DemoExecutionResponse, summary="Iniciar una DEMO",
             description=(
                 "Operador o administrador. Valida vehículo y DEMO activos, pertenencia, comandos "
                 "y N movimientos de sp_obtener_movimientos_demo. Reserva el vehículo y devuelve "
                 "un UUID. Cada paso confirma sp_actualizar_estado antes de notificar. Nunca usa "
                 "sp_registrar_historial. 409 si el vehículo está ocupado o la secuencia no es válida. "
                 "202 acepta una ejecución lógica; no confirma actuación física del microcontrolador."
             ))
async def iniciar_demo(id_vehiculo: IdRuta, id_demo: IdRuta):
    # shield impide que el cierre del cliente deje una reserva o un worker huérfano.
    work = runtime.track_control(asyncio.create_task(asyncio.to_thread(runtime.demos.start, id_vehiculo, id_demo)))
    return await asyncio.shield(work)


@router.post("/{id_vehiculo}/demos/{id_demo}/cancelar", response_model=DemoExecutionResponse,
             summary="Cancelar la DEMO activa",
             description=(
                 "Operador o administrador. Interrumpe las esperas y solicita cancelación cooperativa. "
                 "Espera cualquier operación RDS en curso y el evento terminal antes de responder. "
                 "409 si no hay ejecución activa; 404 si la DEMO no es la activa de ese vehículo. "
                 "El resultado identifica la ejecución; puede ser finalizada o error si terminó antes."
             ))
async def cancelar_demo(id_vehiculo: IdRuta, id_demo: IdRuta):
    return await runtime.demos.cancel(id_vehiculo, id_demo)


@router.post("/{id_vehiculo}/detener", response_model=LogicalStopResponse,
             summary="Detener lógicamente el vehículo en esta API",
             description=(
                 "Operador o administrador. Cancela una DEMO activa y espera sus operaciones RDS. "
                 "Valida el vehículo y publica detencion_logica. No inventa comandos ni modifica "
                 "el estado para simular una parada. No acredita que los motores se hayan detenido."
             ))
async def detener(id_vehiculo: IdRuta):
    # El trabajo de stop continúa administrado aunque se desconecte el cliente HTTP.
    work = runtime.track_control(asyncio.create_task(runtime.demos.stop(id_vehiculo)))
    return await asyncio.shield(work)
