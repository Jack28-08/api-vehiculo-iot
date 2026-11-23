"""Ejecuciones administradas: N pasos confirmados, esperas y cancelación cooperativa."""

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
import threading
from uuid import uuid4

from app.errors import ApplicationError
from app.models.events import VehicleEvent, state_event


@dataclass(eq=False)
class DemoExecution:
    plan: object
    pulse_ticket: str | None = None
    policy: object = None
    id_ejecucion: str = field(default_factory=lambda: str(uuid4()))
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    outcome: str | None = None
    last_state: dict | None = None

    def response(self, state=None):
        return {"id_ejecucion": self.id_ejecucion, "id_vehiculo": self.plan.id_vehiculo,
                "id_demo": self.plan.id_demo, "total_movimientos": self.plan.total_movimientos,
                "estado": state or self.outcome or "iniciada"}


class DemoExecutionManager:
    def __init__(self, coordinator, data_service, connection_manager, *, continuous=None):
        self.coordinator = coordinator
        self.data = data_service
        self.connections = connection_manager
        self._continuous = continuous
        self._loop = None
        self._closing = False
        self._lifecycle_lock = threading.RLock()
        self.tasks: dict[str, asyncio.Task] = {}

    def bind_loop(self, loop=None):
        self._loop = loop or asyncio.get_running_loop()
        self._closing = False

    def _publish(self, event):
        try:
            self.connections.publish_threadsafe(event)
        except Exception:
            pass  # Un cliente de monitoreo no modifica el resultado de RDS.

    def _event(self, execution, kind, state=None, **metadata):
        plan = execution.plan
        return state_event(kind, plan.id_vehiculo, state,
                           id_ejecucion=execution.id_ejecucion, id_demo=plan.id_demo,
                           origen_demo="comercial" if getattr(plan, "commercial", False) else None,
                           total_movimientos=plan.total_movimientos, **metadata)

    def start(self, id_vehiculo, id_demo, *, barrier=None, policy=None, prepared=None):
        """Método síncrono: la ruta lo llama en un worker, nunca en el bucle."""
        if self._closing or self._loop is None:
            raise ApplicationError(503, "El ejecutor de DEMO no está disponible.", "demo_unavailable")
        continuous = self._continuous
        ticket = continuous.pulse_ticket(id_vehiculo, barrier) if continuous is not None else None
        self.coordinator.ensure_available(id_vehiculo)
        with self.coordinator.lock_for(id_vehiculo):
            self.coordinator.ensure_available(id_vehiculo)
            if ticket is not None:
                continuous.pulse_ticket(id_vehiculo, ticket)
            plan = prepared if prepared is not None else self.data.prepare(id_vehiculo, id_demo)
            if policy is not None:
                for movement in plan.movimientos:
                    policy(movement["codigo"])
            if ticket is not None:
                continuous.pulse_ticket(id_vehiculo, ticket)
                for movement in plan.movimientos:
                    continuous.validate_pulse_code(movement.get("codigo"), movement.get("comando"))
            execution = DemoExecution(plan, pulse_ticket=ticket, policy=policy)
            with self._lifecycle_lock:
                if self._closing:
                    raise ApplicationError(503, "El ejecutor de DEMO está cerrando.", "demo_unavailable")
                self.coordinator.reserve(id_vehiculo, execution)
            ready: Future = Future()

            def launch():
                try:
                    task = asyncio.create_task(self._run(execution), name=f"demo-{execution.id_ejecucion}")
                    execution.task = task
                    self.tasks[execution.id_ejecucion] = task
                    task.add_done_callback(lambda finished: self.tasks.pop(execution.id_ejecucion, None))
                    execution.ready.set()
                    ready.set_result(None)
                except Exception:
                    execution.ready.set()
                    ready.set_exception(RuntimeError("No se pudo iniciar la tarea DEMO."))

            try:
                if ticket is not None:
                    continuous.validate_pulse_ticket(id_vehiculo, ticket)
                self._loop.call_soon_threadsafe(launch)
                ready.result()
            except ApplicationError:
                execution.outcome = "cancelada" if execution.cancel_requested.is_set() else "error"
                self.coordinator.clear_if_owner(id_vehiculo, execution.id_ejecucion)
                self._loop.call_soon_threadsafe(execution.ready.set)
                raise
            except Exception:
                self.coordinator.clear_if_owner(id_vehiculo, execution.id_ejecucion)
                raise ApplicationError(503, "No se pudo iniciar la DEMO.", "demo_unavailable") from None
            # El runner no puede confirmar un paso mientras este bloqueo siga tomado.
            self._publish(self._event(execution, "demo_inicio"))
            return execution.response("iniciada")

    def _step(self, execution, movement):
        plan = execution.plan
        ticket = execution.pulse_ticket
        operation_id = uuid4()
        with self.coordinator.lock_for(plan.id_vehiculo):
            if execution.cancel_requested.is_set() or self.coordinator.get_active(plan.id_vehiculo) is not execution:
                return False
            # Al volver, la transacción ya fue confirmada y cerrada.
            if ticket is None and execution.policy is None:
                state = self.data.step(plan.id_vehiculo, plan.id_demo, movement)
                dispatched = True
            else:
                continuous = self._continuous
                if ticket is not None:
                    continuous.validate_pulse_ticket(plan.id_vehiculo, ticket)

                def validate_command(code):
                    if ticket is not None:
                        continuous.validate_pulse_ticket(plan.id_vehiculo, ticket)
                        continuous.validate_pulse_code(code)
                    if code != movement.get("codigo"):
                        raise ApplicationError(409, "El catálogo cambió desde que se preparó la DEMO.", "changed_demo_code")

                kwargs = {"commercial": True} if getattr(plan, "commercial", False) else {}
                if execution.policy is not None:
                    kwargs["platform_policy"] = execution.policy
                state = self.data.step(plan.id_vehiculo, plan.id_demo, movement, before_execute=validate_command, **kwargs)
                dispatched = ticket is None or continuous.dispatch_pulse(
                    plan.id_vehiculo, ticket, state.get("codigo"),
                    movement["duracion_ms"], name=state.get("comando"), operation=operation_id,
                )
            execution.last_state = state
            self._publish(self._event(
                execution, "demo_movimiento", state, orden=movement["orden"],
                duracion_ms=movement["duracion_ms"], pausa_ms=movement["pausa_ms"], id_operacion=operation_id,
            ))
            if not dispatched:
                if execution.cancel_requested.is_set() or self.coordinator.stopping(plan.id_vehiculo):
                    return False
                raise ApplicationError(503, "El paso no se entregó al dispositivo.", "pulse_unavailable")
            return True

    async def _wait(self, execution, milliseconds):
        if execution.cancel_requested.is_set():
            return True
        if milliseconds and execution.policy is not None:
            end = asyncio.get_running_loop().time() + milliseconds / 1000
            while (remaining := end - asyncio.get_running_loop().time()) > 0:
                try:
                    await asyncio.wait_for(execution.wake.wait(), min(remaining, .45))
                    return True
                except TimeoutError:
                    if remaining > .45:
                        try:
                            await asyncio.to_thread(execution.policy)
                        except Exception:
                            if execution.pulse_ticket is not None:
                                self._continuous.stop(execution.plan.id_vehiculo)
                            execution.cancel_requested.set()
                            return True
        elif milliseconds:
            try:
                await asyncio.wait_for(execution.wake.wait(), milliseconds / 1000)
            except TimeoutError:
                pass
        return execution.cancel_requested.is_set()

    async def _worker(self, execution, function, *args):
        work = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # Incluso una cancelación externa de la tarea espera al hilo en curso.
            execution.cancel_requested.set()
            execution.wake.set()
            await asyncio.shield(work)
            raise

    async def _run(self, execution):
        outcome = "finalizada"
        try:
            for movement in execution.plan.movimientos:
                if not await self._worker(execution, self._step, execution, movement):
                    outcome = "cancelada"
                    break
                if await self._wait(execution, movement["duracion_ms"]):
                    outcome = "cancelada"
                    break
                if execution.pulse_ticket is not None:
                    self._continuous.end_demo_step(execution.pulse_ticket)
                if await self._wait(execution, movement["pausa_ms"]):
                    outcome = "cancelada"
                    break
        except asyncio.CancelledError:
            outcome = "cancelada"
        except Exception:
            outcome = "cancelada" if execution.cancel_requested.is_set() else "error"
        await asyncio.to_thread(self._finish, execution, outcome)
        return execution.response()

    def _finish(self, execution, outcome):
        identifier = execution.plan.id_vehiculo
        with self.coordinator.lock_for(identifier):
            if execution.outcome is not None:
                return
            if outcome == "finalizada" and execution.cancel_requested.is_set():
                outcome = "cancelada"
            if outcome == "finalizada" and execution.pulse_ticket is not None:
                try:
                    self._continuous.validate_pulse_ticket(identifier, execution.pulse_ticket)
                except ApplicationError:
                    outcome = "cancelada" if self.coordinator.stopping(identifier) else "error"
            execution.outcome = outcome
            kind = {"finalizada": "demo_fin", "cancelada": "demo_cancelada", "error": "demo_error"}[outcome]
            message = "No se pudo completar la DEMO." if outcome == "error" else None
            event = self._event(execution, kind, mensaje=message)
            if execution.pulse_ticket is not None:
                self._continuous.cancel_demo_pulses(identifier, execution.pulse_ticket)
            # Al observar el desenlace, el vehículo ya debe estar disponible.
            # Se mantiene el mutex hasta encolar el evento: una ejecución nueva
            # no puede publicar su inicio antes del desenlace de la anterior.
            self.coordinator.clear_if_owner(identifier, execution.id_ejecucion)
            self._publish(event)

    async def _cancel_execution(self, execution):
        execution.cancel_requested.set()
        if execution.pulse_ticket is not None:
            self._continuous.cancel_demo_pulses(execution.plan.id_vehiculo, execution.pulse_ticket)
        execution.wake.set()
        await execution.ready.wait()
        if execution.task is not None:
            await asyncio.shield(execution.task)
        return execution.response()

    def signal_cancel(self, id_vehiculo):
        """Revoca desde una transacción administrativa sin esperar al worker RDS."""
        execution = self.coordinator.get_active(id_vehiculo)
        if execution is None:
            return
        execution.cancel_requested.set()
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(execution.wake.set)

    async def cancel(self, id_vehiculo, id_demo=None):
        execution = self.coordinator.get_active(id_vehiculo)
        if execution is None:
            if self._continuous is not None:
                self._continuous.cancel_demo_pulses(id_vehiculo, None)
            raise ApplicationError(409, "El vehículo no tiene una DEMO en curso.", "demo_not_running")
        if id_demo is not None and execution.plan.id_demo != id_demo:
            raise ApplicationError(404, "La DEMO indicada no es la ejecución activa del vehículo.", "demo_not_found")
        return await self._cancel_execution(execution)

    async def stop(self, id_vehiculo):
        # Se marca antes de esperar RDS; no bloquea el bucle y da prioridad al stop.
        self.coordinator.begin_stop(id_vehiculo)
        try:
            operation_id = self._continuous.stop(id_vehiculo) if self._continuous is not None else None
            execution = self.coordinator.get_active(id_vehiculo)
            if execution is not None:
                await self._cancel_execution(execution)

            def acknowledge():
                with self.coordinator.lock_for(id_vehiculo):
                    state = self.data.snapshot(id_vehiculo)
                    self._publish(state_event(
                        "detencion_logica", id_vehiculo, state,
                        id_ejecucion=execution.id_ejecucion if execution else None,
                        id_operacion=operation_id,
                        mensaje="Detención lógica de la API; sin confirmación física de motores.",
                    ))

            # No se responde hasta que los workers anteriores liberaron el vehículo.
            await asyncio.to_thread(acknowledge)
            return {"id_vehiculo": id_vehiculo, "detencion_logica": True,
                    "id_ejecucion": execution.id_ejecucion if execution else None,
                    "id_operacion": operation_id,
                    "estado": "detenida_logicamente"}
        finally:
            self.coordinator.end_stop(id_vehiculo)

    async def shutdown(self):
        with self._lifecycle_lock:
            self._closing = True
            active = self.coordinator.active_executions()
        if active:
            await asyncio.gather(*(self._cancel_execution(e) for e in active), return_exceptions=True)
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
