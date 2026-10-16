"""Casos de uso MySQL de DEMO, independientes de tareas y WebSocket."""

from dataclasses import dataclass

from pydantic import ValidationError

from app.errors import ApplicationError
from app.models.vehiculos import DemoMovimientoCrear, EstadoRespuesta
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
    def _active(row, label):
        if not row["activo"]:
            raise ApplicationError(409, f"{label} está inactivo.", "inactive_resource")

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
            self._active(self._vehiculo(connection, id_vehiculo), "El vehículo")
            self._active(self._demo(connection, id_vehiculo, id_demo), "La DEMO")
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
                self._active(command, "El comando")
                orders.append(movement["orden"])
            if len(set(orders)) != len(orders) or orders != sorted(orders):
                raise ApplicationError(409, "El orden de la DEMO no es válido.", "invalid_demo")
            return DemoPlan(id_vehiculo, id_demo, tuple(dict(r) for r in rows))

    def step(self, id_vehiculo, id_demo, movement):
        self._valid_movement(movement)
        with self.manager.transaction() as connection:
            self._active(self._vehiculo(connection, id_vehiculo, lock=True), "El vehículo")
            self._active(self._demo(connection, id_vehiculo, id_demo), "La DEMO")
            command = self._comando(connection, id_vehiculo, movement["id_comando"], lock=True)
            self._active(command, "El comando")
            previous = self.repository.bloquear_estado(connection, id_vehiculo)
            connected = bool(previous["conectado"]) if previous is not None else False
            self.repository.actualizar_estado(connection, id_vehiculo, movement["id_comando"], connected)
            state = self.repository.obtener_estado(connection, id_vehiculo)
            if state is None:
                raise ApplicationError(500, "No se pudo confirmar el estado de la DEMO.", "result_missing")
            result = EstadoRespuesta.model_validate(state).model_dump()
        return result  # El contexto ya confirmó y cerró antes de devolver.
