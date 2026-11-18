"""Control continuo HTTP y canal de dispositivo autenticado aparte del monitor."""

import asyncio
from contextlib import suppress

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.models.continuous import Capability, ControlGrant, ControlVector, DeviceHeartbeat, Gesture, ReleaseResult
from app.routes.vehiculos import IdRuta
from app.runtime import runtime
from app.security import require_operator
from app.services.continuous import DEVICE_TIMEOUT


router = APIRouter(tags=["Control continuo"])
operator = [Depends(require_operator)]
MAX_HEARTBEAT_BYTES = 128
SEND_TIMEOUT_SECONDS = .25


@router.get("/vehiculos/{id_vehiculo}/control/capacidad", response_model=Capability)
def capability(id_vehiculo: IdRuta, response: Response):
    """Informa capacidades del broker y presencia reciente de su canal de equipo."""
    response.headers["Cache-Control"] = "no-store"
    return runtime.continuous.capability(id_vehiculo)


@router.post("/vehiculos/{id_vehiculo}/control/permiso", dependencies=operator, response_model=ControlGrant)
def acquire(id_vehiculo: IdRuta, vector: ControlVector, response: Response):
    """Reserva un permiso con secuencia inicial 1. Mantiene la consigna neutral."""
    response.headers["Cache-Control"] = "no-store"
    return runtime.continuous.acquire(id_vehiculo, vector)


@router.post("/vehiculos/{id_vehiculo}/control/vector", dependencies=operator, response_model=ControlGrant)
def update(id_vehiculo: IdRuta, vector: ControlVector, response: Response):
    """Modifica la consigna efímera con una secuencia estrictamente creciente."""
    response.headers["Cache-Control"] = "no-store"
    return runtime.continuous.update(id_vehiculo, vector)


@router.post("/vehiculos/{id_vehiculo}/control/liberar", dependencies=operator, response_model=ReleaseResult)
def release(id_vehiculo: IdRuta, gesture: Gesture, response: Response):
    """Invalida la barrera del gesto para rechazar mensajes que lleguen después."""
    response.headers["Cache-Control"] = "no-store"
    return runtime.continuous.release(id_vehiculo, gesture)


@router.websocket("/ws/dispositivos/{id_vehiculo}")
async def device(websocket: WebSocket, id_vehiculo: IdRuta):
    broker = runtime.continuous
    credentials = websocket.headers.getlist("authorization")
    if len(credentials) != 1 or not broker.authenticates(id_vehiculo, credentials[0]):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    connection = broker.connect()
    close_code = None
    try:
        while True:
            message = await asyncio.wait_for(websocket.receive(), DEVICE_TIMEOUT)
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if not text or not text.isascii() or len(text) > MAX_HEARTBEAT_BYTES:
                raise ValueError("Latido inválido.")
            heartbeat = DeviceHeartbeat.model_validate_json(text)
            snapshot = broker.heartbeat(connection, heartbeat.reto)
            # El dispositivo solicita una consigna por latido. No hay cola de órdenes para reintentar.
            await asyncio.wait_for(websocket.send_json(snapshot.model_dump(mode="json")), SEND_TIMEOUT_SECONDS)
    except (ValidationError, ValueError):
        close_code = 1008
    except TimeoutError:
        close_code = 1013
    except WebSocketDisconnect:
        pass
    finally:
        broker.disconnect(connection)
        if close_code is not None:
            with suppress(Exception):
                await asyncio.wait_for(websocket.close(code=close_code), SEND_TIMEOUT_SECONDS)
