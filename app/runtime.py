"""Componentes compartidos únicamente dentro del proceso local de la aplicación."""

from time import perf_counter
from uuid import uuid4

from app.database.connection import transition_timing
from app.models.events import state_event
from app.services.continuous import ContinuousControl
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
            continuous = self.runtime.continuous
            barrier = kwargs.pop("control_barrier", None)
            platform_policy = kwargs.pop("platform_policy", None)
            movement = name == "ejecutar_movimiento"
            request = (args[0] if args else kwargs.get("data")) if movement else None
            request_id = getattr(request, "id_solicitud", None)
            ticket = continuous.pulse_ticket(id_vehiculo, barrier) if movement else None
            self.runtime.coordinator.ensure_available(id_vehiculo)
            with self.runtime.coordinator.lock_for(id_vehiculo), self.runtime.coordinator.legacy_mutation(id_vehiculo):
                if ticket is not None:
                    continuous.pulse_ticket(id_vehiculo, ticket)
                    continuous.claim_pulse_request(id_vehiculo, ticket, request_id)

                    def validate_command(code):
                        continuous.validate_pulse_ticket(id_vehiculo, ticket)
                        continuous.validate_pulse_code(code)
                    kwargs["before_execute"] = validate_command
                elif name in {"actualizar_vehiculo", "eliminar_vehiculo", "crear_comando",
                              "actualizar_comando", "eliminar_comando"}:
                    # Una modificación de catálogo invalida solicitudes que vieron sus datos anteriores.
                    continuous.stop(id_vehiculo)
                if platform_policy is not None:
                    kwargs["platform_policy"] = platform_policy
                result = operation(id_vehiculo, *args, **kwargs)
                if movement:
                    operation_id = request_id or uuid4()
                    if ticket is not None:
                        continuous.dispatch_pulse(id_vehiculo, ticket, result.estado.codigo,
                                                  name=result.estado.comando, operation=operation_id)
                    self.runtime.publish_movement(id_vehiculo, result.estado, result.historial.id_historial,
                                                  operation_id, result.historial.fecha_hora)
                    result = result.model_copy(update={"id_operacion": str(operation_id)})
                return result
        return mutate


class AppRuntime:
    def __init__(self, data_service=None, connections=None, *, device_settings=None):
        self.coordinator = VehicleCoordinator()
        self.connections = connections if connections is not None else ConnectionManager()
        self.data = data_service if data_service is not None else DemoDataService()
        self.continuous = ContinuousControl(self.coordinator, device_settings, record_transition=self.record_transition)
        self.demos = DemoExecutionManager(self.coordinator, self.data, self.connections, continuous=self.continuous)
        self.control_tasks = set()

    def publish_movement(self, vehicle_id, state, history_id, operation_id, timestamp=None):
        try:
            event = state_event("movimiento_individual", vehicle_id, state.model_dump(),
                                id_operacion=operation_id, id_historial=history_id)
            event.fecha_hora = timestamp or event.fecha_hora
            self.connections.publish_threadsafe(type(event).model_validate(event.model_dump()))
        except Exception:
            pass  # El commit ya se confirmó, independientemente del monitor.

    def record_transition(self, vehicle_id, code, names, operation_id, validate):
        measurements = {}
        with self.coordinator.lock_for(vehicle_id):
            validate()
            timing_token = transition_timing.set(measurements)
            try:
                state, history_id = self.data.ejecutar_transicion(vehicle_id, code, names, validate)
            finally:
                transition_timing.reset(timing_token)
            started = perf_counter()
            self.publish_movement(vehicle_id, state, history_id, operation_id)
            measurements["publicacion_wss"] = round((perf_counter() - started) * 1000)
        return history_id, measurements

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
            socket_scope = getattr(websocket, "scope", {})
            state = None if socket_scope.get("iot43_guest") else self.data.snapshot(id_vehiculo)
            history_policy = socket_scope.get("iot43_history_policy")
            if state and history_policy is not None and not history_policy(state.get("fecha_actualizacion")):
                state = None
            return self.connections.register_threadsafe(
                websocket, id_vehiculo, state_event("estado_inicial", id_vehiculo, state),
            )

    async def shutdown(self):
        self.continuous.close()
        await self.demos.shutdown()
        if self.control_tasks:
            import asyncio
            await asyncio.gather(*tuple(self.control_tasks), return_exceptions=True)
        await self.connections.shutdown()


runtime = AppRuntime()
