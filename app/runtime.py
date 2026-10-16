"""Componentes compartidos únicamente dentro del proceso local de la aplicación."""

from app.models.events import state_event
from app.services.coordination import VehicleCoordinator
from app.services.demo_data import DemoDataService
from app.services.demos import DemoExecutionManager
from app.services.vehiculos import VehiculoService
from app.websocket.manager import ConnectionManager


class CoordinatedVehiculoService:
    """Adapta los casos HTTP existentes al mismo bloqueo que usa DEMO."""

    def __init__(self, runtime, service=None):
        self.runtime = runtime
        self.service = service if service is not None else VehiculoService()

    def __getattr__(self, name):
        operation = getattr(self.service, name)
        if name.startswith(("obtener_", "listar_")) or name == "crear_vehiculo":
            return operation

        def mutate(id_vehiculo, *args, **kwargs):
            self.runtime.coordinator.ensure_available(id_vehiculo)
            with self.runtime.coordinator.lock_for(id_vehiculo):
                self.runtime.coordinator.ensure_available(id_vehiculo)
                result = operation(id_vehiculo, *args, **kwargs)
                if name == "ejecutar_movimiento":
                    # El servicio solo retorna después de su commit; fallos no llegan aquí.
                    try:
                        event = state_event("movimiento_individual", id_vehiculo,
                                            result.estado.model_dump())
                        event.fecha_hora = result.historial.fecha_hora or event.fecha_hora
                        # Validar de nuevo conserva UTC incluso al usar la fecha del historial.
                        self.runtime.connections.publish_threadsafe(type(event).model_validate(event.model_dump()))
                    except Exception:
                        pass  # Nunca presentar un commit confirmado como escritura fallida.
                return result
        return mutate


class AppRuntime:
    def __init__(self, data_service=None, connections=None):
        self.coordinator = VehicleCoordinator()
        self.connections = connections if connections is not None else ConnectionManager()
        self.data = data_service if data_service is not None else DemoDataService()
        self.demos = DemoExecutionManager(self.coordinator, self.data, self.connections)
        self.control_tasks = set()

    def bind_loop(self):
        self.connections.bind_loop()
        self.demos.bind_loop()

    def track_control(self, task):
        self.control_tasks.add(task)

        def finished(done):
            self.control_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Consume excepciones si el cliente HTTP ya se desconectó.

        task.add_done_callback(finished)
        return task

    def subscribe(self, websocket, id_vehiculo):
        # Snapshot y registro se ordenan con todos los commits y eventos del vehículo.
        with self.coordinator.lock_for(id_vehiculo):
            state = self.data.snapshot(id_vehiculo)
            return self.connections.register_threadsafe(
                websocket, id_vehiculo, state_event("estado_inicial", id_vehiculo, state),
            )

    async def shutdown(self):
        await self.demos.shutdown()
        if self.control_tasks:
            import asyncio
            await asyncio.gather(*tuple(self.control_tasks), return_exceptions=True)
        await self.connections.shutdown()


runtime = AppRuntime()
