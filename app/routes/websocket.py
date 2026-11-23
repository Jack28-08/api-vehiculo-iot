"""WebSocket público de monitoreo. Mensajes entrantes nunca ejecutan acciones."""

import asyncio
from functools import partial

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database.connection import DatabaseUnavailableError
from app.errors import ApplicationError
from app.routes.vehiculos import IdRuta
from app.runtime import runtime
from app.platform43.access import open_monitor, validate_scope, visible_since_assignment
from app.platform43 import auth


class WebSocketController:
    """Controlador instanciado: sus métodos enlazados atienden las rutas."""

    def __init__(self) -> None:
        self._router = APIRouter()
        self._router.add_api_websocket_route("/ws/vehiculos/{id_vehiculo}", self.monitor_vehicle)

    @property
    def router(self) -> APIRouter:
        return self._router

    async def monitor_vehicle(self, websocket: WebSocket, id_vehiculo: IdRuta):
        guard = None
        try:
            if auth.platform_enabled():
                scope = await asyncio.to_thread(open_monitor, websocket, id_vehiculo)
                guard = partial(validate_scope, scope)
                websocket.scope["iot43_sender_guard"] = guard
                websocket.scope["iot43_guest"] = scope.guest
                websocket.scope["iot43_history_policy"] = partial(visible_since_assignment, scope)
        except ApplicationError:
            await websocket.close(code=4403)
            return
        except DatabaseUnavailableError:
            await websocket.close(code=1013)
            return
        if guard is None:
            await websocket.accept()
        else:
            await websocket.accept(subprotocol="node01")
        client = None
        registration = asyncio.create_task(asyncio.to_thread(runtime.subscribe, websocket, id_vehiculo))
        try:
            client = await asyncio.shield(registration)
            while True:
                try:
                    message = (await websocket.receive() if guard is None else
                               await asyncio.wait_for(websocket.receive(), 3))
                except TimeoutError:
                    try:
                        await asyncio.to_thread(guard)
                    except (ApplicationError, DatabaseUnavailableError):
                        await runtime.connections.disconnect(client, 4403)
                        break
                    continue
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


controller = WebSocketController()
router = controller.router
