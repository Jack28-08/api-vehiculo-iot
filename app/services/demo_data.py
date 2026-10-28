"""Casos de uso MySQL de DEMO, independientes de tareas y WebSocket."""

from dataclasses import dataclass

from pydantic import ValidationError

from app.domain import Comando, Demo, Vehiculo
from app.errors import ApplicationError
from app.models.vehiculos import DemoMovimientoCrear, EstadoRespuesta
from app.services.movimientos import MovimientoDeDemo
from app.services.vehiculos import VehiculoService


@dataclass(frozen=True)
class DemoPlan:
    id_vehiculo: int
    id_demo: int
    movimientos: tuple[dict, ...]

    @property
    def total_movimientos(self):
        return len(self.movimientos)


class DemoDataService(VehiculoService):
    """Prepara una instantánea y confirma un paso por transacción, sin historial."""

    @staticmethod
    def _valid_movement(movement):
        try:
            DemoMovimientoCrear.model_validate({k: movement[k] for k in (
                "id_comando", "orden", "duracion_ms", "pausa_ms",
            )})
        except (ValidationError, KeyError):
            raise ApplicationError(409, "La DEMO contiene movimientos inválidos.", "invalid_demo") from None

    def snapshot(self, id_vehiculo):
        with self.manager.transaction(readonly=True) as connection:
            self._vehiculo(connection, id_vehiculo)
            row = self.repository.obtener_estado(connection, id_vehiculo)
            return EstadoRespuesta.model_validate(row).model_dump() if row is not None else None

    def prepare(self, id_vehiculo, id_demo):
        with self.manager.transaction(readonly=True) as connection:
            vehicle = Vehiculo.desde_fila(self._vehiculo(connection, id_vehiculo))
            vehicle.exigir_activo()
            demo = Demo.desde_fila(self._demo(connection, id_vehiculo, id_demo))
            demo.exigir_pertenencia(vehicle)
            demo.exigir_activa()
            rows = self.repository.obtener_movimientos_demo(connection, id_demo)
            if not rows:
                raise ApplicationError(409, "La DEMO no contiene movimientos.", "empty_demo")
            commands = {r["id_comando"]: r for r in self.repository.obtener_comandos(connection, id_vehiculo)}
            orders = []
            for movement in rows:
                self._valid_movement(movement)
                command = commands.get(movement["id_comando"])
                if command is None or command["id_vehiculo"] != id_vehiculo:
                    raise ApplicationError(409, "La DEMO contiene un comando ajeno o inexistente.", "invalid_demo")
                Comando.desde_fila(command).exigir_activo()
                orders.append(movement["orden"])
            if len(set(orders)) != len(orders) or orders != sorted(orders):
                raise ApplicationError(409, "El orden de la DEMO no es válido.", "invalid_demo")
            return DemoPlan(id_vehiculo, id_demo, tuple(dict(r) for r in rows))

    def step(self, id_vehiculo, id_demo, movement):
        self._valid_movement(movement)
        with self.manager.transaction() as connection:
            vehicle = Vehiculo.desde_fila(self._vehiculo(connection, id_vehiculo, lock=True))
            vehicle.exigir_activo()
            demo = Demo.desde_fila(self._demo(connection, id_vehiculo, id_demo))
            demo.exigir_pertenencia(vehicle)
            demo.exigir_activa()
            command = Comando.desde_fila(
                self._comando(connection, id_vehiculo, movement["id_comando"], lock=True),
            )
            operation = MovimientoDeDemo(vehicle, command)
            confirmed = operation.ejecutar(self.repository, connection)
            result = EstadoRespuesta.model_validate(confirmed.estado).model_dump()
        return result  # El contexto ya confirmó y cerró antes de devolver.
