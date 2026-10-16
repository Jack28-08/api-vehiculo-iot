"""Distribución por vehículo, con una cola acotada y un emisor por cliente."""

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
import threading
from typing import Any

from app.models.events import VehicleEvent


@dataclass(eq=False)
class ClientConnection:
    websocket: Any
    id_vehiculo: int
    queue: asyncio.Queue
    sender_task: asyncio.Task | None = None
    closed: bool = False
    close_started: bool = False


class ConnectionManager:
    """Solo el bucle de la aplicación modifica clientes y colas.

    Los métodos threadsafe confirman encolado, nunca esperan un envío de red.
    La coordinación por vehículo serializa esos métodos con los commits de RDS.
    """

    def __init__(self, max_queue_size: int = 64, send_timeout: float = 2.0):
        if max_queue_size < 1 or send_timeout <= 0:
            raise ValueError("Límites de WebSocket inválidos.")
        self.max_queue_size = max_queue_size
        self.send_timeout = send_timeout
        self.clients: dict[int, set[ClientConnection]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop = None
        self._thread = None
        self._closing = False

    def bind_loop(self, loop=None):
        self._loop = loop or asyncio.get_running_loop()
        self._thread = threading.get_ident()
        self._closing = False

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _register(self, websocket, id_vehiculo: int, initial_event: VehicleEvent):
        if self._closing:
            raise RuntimeError("El monitor está cerrando.")
        client = ClientConnection(websocket, id_vehiculo, asyncio.Queue(self.max_queue_size))
        # No hay await entre registro y estado inicial: ningún evento puede adelantarse.
        client.queue.put_nowait(initial_event.model_dump(mode="json"))
        self.clients.setdefault(id_vehiculo, set()).add(client)
        client.sender_task = self._task(self._sender(client))
        return client

    async def register(self, websocket, id_vehiculo: int, initial_event: VehicleEvent):
        return self._register(websocket, id_vehiculo, initial_event)

    def register_threadsafe(self, websocket, id_vehiculo: int, initial_event: VehicleEvent):
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("El monitor no está disponible.")
        if threading.get_ident() == self._thread:
            return self._register(websocket, id_vehiculo, initial_event)
        # Sin timeout: el worker mantiene el bloqueo hasta que el registro queda ordenado.
        future = asyncio.run_coroutine_threadsafe(self.register(websocket, id_vehiculo, initial_event), self._loop)
        return future.result()

    def _publish(self, event: VehicleEvent) -> bool:
        if self._closing:
            return False
        payload = event.model_dump(mode="json")
        for client in tuple(self.clients.get(event.id_vehiculo, ())):
            if client.closed:
                continue
            try:
                client.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._detach(client)
                self._task(self._terminate(client, 1013))
        return True

    async def publish(self, event: VehicleEvent) -> bool:
        return self._publish(event)

    def publish_threadsafe(self, event: VehicleEvent) -> bool:
        """Una notificación fallida nunca cambia el resultado de un commit previo."""
        try:
            if self._loop is None or self._loop.is_closed() or self._closing:
                return False
            if threading.get_ident() == self._thread:
                return self._publish(event)
            future: Future = Future()

            def enqueue():
                try:
                    future.set_result(self._publish(event))
                except Exception:
                    future.set_result(False)

            self._loop.call_soon_threadsafe(enqueue)
            return future.result()
        except Exception:
            return False

    def _detach(self, client):
        client.closed = True
        group = self.clients.get(client.id_vehiculo)
        if group is not None:
            group.discard(client)
            if not group:
                self.clients.pop(client.id_vehiculo, None)

    async def _terminate(self, client, code):
        self._detach(client)
        if client.close_started:
            return
        client.close_started = True
        sender = client.sender_task
        if sender is not None and sender is not asyncio.current_task() and not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        try:
            await asyncio.wait_for(client.websocket.close(code=code), self.send_timeout)
        except Exception:
            pass

    async def _sender(self, client):
        try:
            while not client.closed:
                payload = await client.queue.get()
                await asyncio.wait_for(client.websocket.send_json(payload), self.send_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._terminate(client, 1013)

    async def disconnect(self, client, code=1000):
        await self._terminate(client, code)

    async def shutdown(self):
        self._closing = True
        clients = [client for group in self.clients.values() for client in group]
        await asyncio.gather(*(self.disconnect(c, 1001) for c in clients), return_exceptions=True)
        pending = tuple(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._loop = None
