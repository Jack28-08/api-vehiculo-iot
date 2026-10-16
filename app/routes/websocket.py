"""WebSocket público de monitoreo. Mensajes entrantes nunca ejecutan acciones."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database.connection import DatabaseUnavailableError
from app.errors import ApplicationError
from app.routes.vehiculos import IdRuta
from app.runtime import runtime

router = APIRouter()


@router.websocket("/ws/vehiculos/{id_vehiculo}")
async def monitor_vehicle(websocket: WebSocket, id_vehiculo: IdRuta):
    await websocket.accept()
    client = None
    registration = asyncio.create_task(asyncio.to_thread(runtime.subscribe, websocket, id_vehiculo))
    try:
        client = await asyncio.shield(registration)
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            # Monitor solamente: ni texto ni JSON del cliente producen comandos o escrituras.
    except asyncio.CancelledError:
        try:
            client = await asyncio.shield(registration)
        except Exception:
            pass
        raise
    except WebSocketDisconnect:
        pass
    except ApplicationError:
        await websocket.close(code=4404, reason="Vehículo no encontrado.")
    except DatabaseUnavailableError:
        await websocket.close(code=1013, reason="Monitoreo temporalmente no disponible.")
    except Exception:
        try:
            await websocket.close(code=1011, reason="No fue posible abrir el monitor.")
        except Exception:
            pass
    finally:
        if client is not None:
            await runtime.connections.disconnect(client)
