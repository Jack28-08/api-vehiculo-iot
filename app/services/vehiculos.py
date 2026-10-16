"""Casos de uso: validación de pertenencia y propiedad de las transacciones."""

from typing import Any

from app.database.connection import MySQLConnectionManager
from app.errors import ApplicationError
from app.models.vehiculos import (
    ComandoActualizar, ComandoCrear, ComandoRespuesta,
    DemoActualizar, DemoCrear, DemoMovimientoActualizar, DemoMovimientoCrear,
    DemoMovimientoRespuesta, DemoRespuesta, EliminacionRespuesta, EstadoRespuesta,
    HistorialRespuesta, MovimientoConfirmado, MovimientoEjecutar,
    VehiculoActualizar, VehiculoCrear, VehiculoRespuesta,
)
from app.repositories.vehiculos import VehiculoRepository


class VehiculoService:
    """Coordina SP usando una conexión por caso de uso y una única confirmación.

    Las consultas usan transacciones de lectura. Las escrituras bloquean la fila
    del vehículo para serializar cambios relacionados dentro de esta API. No hay
    commits en repositorios, ni conexiones compartidas entre solicitudes.
    """

    def __init__(self, manager: MySQLConnectionManager | None = None,
                 repository: VehiculoRepository | None = None) -> None:
        self.manager = manager if manager is not None else MySQLConnectionManager()
        self.repository = repository if repository is not None else VehiculoRepository(self.manager)

    def _vehiculo(self, connection: Any, id_vehiculo: int, *, lock: bool = False) -> dict:
        if lock:
            self.repository.bloquear_vehiculo(connection, id_vehiculo)
        row = self.repository.obtener_vehiculo(connection, id_vehiculo)
        if row is None:
            raise ApplicationError(404, "Vehículo no encontrado.", "vehicle_not_found")
        return row

    def _comando(self, connection: Any, id_vehiculo: int, id_comando: int,
                 *, lock: bool = False) -> dict:
        if lock:
            self.repository.bloquear_comando(connection, id_comando)
        rows = self.repository.obtener_comandos(connection, id_vehiculo)
        row = next((r for r in rows if r["id_comando"] == id_comando), None)
        if row is None:
            raise ApplicationError(404, "Comando no encontrado en este vehículo.", "command_not_found")
        return row

    def _demo(self, connection: Any, id_vehiculo: int, id_demo: int) -> dict:
        rows = self.repository.obtener_demos(connection, id_vehiculo)
        row = next((r for r in rows if r["id_demo"] == id_demo), None)
        if row is None:
            raise ApplicationError(404, "DEMO no encontrada en este vehículo.", "demo_not_found")
        return row

    def _movimiento_demo(self, connection: Any, id_demo: int, id_movimiento: int) -> dict:
        rows = self.repository.obtener_movimientos_demo(connection, id_demo)
        row = next((r for r in rows if r["id_demo_movimiento"] == id_movimiento), None)
        if row is None:
            raise ApplicationError(404, "Movimiento no encontrado en esta DEMO.", "demo_movement_not_found")
        return row

    def listar_vehiculos(self) -> list[VehiculoRespuesta]:
        with self.manager.transaction(readonly=True) as connection:
            return [VehiculoRespuesta.model_validate(r)
                    for r in self.repository.listar_vehiculos(connection)]

    def obtener_vehiculo(self, id_vehiculo: int) -> VehiculoRespuesta:
        with self.manager.transaction(readonly=True) as connection:
            return VehiculoRespuesta.model_validate(self._vehiculo(connection, id_vehiculo))

    def obtener_comandos(self, id_vehiculo: int) -> list[ComandoRespuesta]:
        with self.manager.transaction(readonly=True) as connection:
            self._vehiculo(connection, id_vehiculo)
            return [ComandoRespuesta.model_validate(r)
                    for r in self.repository.obtener_comandos(connection, id_vehiculo)]

    def obtener_estado(self, id_vehiculo: int) -> EstadoRespuesta | None:
        with self.manager.transaction(readonly=True) as connection:
            self._vehiculo(connection, id_vehiculo)
            row = self.repository.obtener_estado(connection, id_vehiculo)
            return EstadoRespuesta.model_validate(row) if row is not None else None

    def obtener_historial(self, id_vehiculo: int) -> list[HistorialRespuesta]:
        with self.manager.transaction(readonly=True) as connection:
            self._vehiculo(connection, id_vehiculo)
            # El repositorio ya consumió TODAS las filas/sets. No hay DELETE ni LIMIT en RDS.
            rows = self.repository.obtener_historial(connection, id_vehiculo)
            return [HistorialRespuesta.model_validate(r) for r in rows[:10]]

    def obtener_demos(self, id_vehiculo: int) -> list[DemoRespuesta]:
        with self.manager.transaction(readonly=True) as connection:
            self._vehiculo(connection, id_vehiculo)
            return [DemoRespuesta.model_validate(r)
                    for r in self.repository.obtener_demos(connection, id_vehiculo)]

    def obtener_movimientos_demo(self, id_vehiculo: int, id_demo: int) -> list[DemoMovimientoRespuesta]:
        with self.manager.transaction(readonly=True) as connection:
            self._vehiculo(connection, id_vehiculo)
            self._demo(connection, id_vehiculo, id_demo)
            return [DemoMovimientoRespuesta.model_validate(r)
                    for r in self.repository.obtener_movimientos_demo(connection, id_demo)]

    def crear_vehiculo(self, data: VehiculoCrear) -> VehiculoRespuesta:
        with self.manager.transaction() as connection:
            identifier = self.repository.crear_vehiculo(
                connection, data.nombre, data.modelo, data.descripcion, data.microcontrolador,
            )
            return VehiculoRespuesta.model_validate(self._vehiculo(connection, identifier))

    def actualizar_vehiculo(self, id_vehiculo: int, data: VehiculoActualizar) -> VehiculoRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self.repository.actualizar_vehiculo(
                connection, id_vehiculo, data.nombre, data.modelo, data.descripcion,
                data.microcontrolador, data.activo,
            )
            return VehiculoRespuesta.model_validate(self._vehiculo(connection, id_vehiculo))

    def eliminar_vehiculo(self, id_vehiculo: int) -> EliminacionRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self.repository.eliminar_vehiculo(connection, id_vehiculo)
            return EliminacionRespuesta(eliminado=True, id=id_vehiculo)

    def crear_comando(self, id_vehiculo: int, data: ComandoCrear) -> ComandoRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            identifier = self.repository.crear_comando(
                connection, id_vehiculo, data.nombre, data.codigo, data.descripcion,
            )
            return ComandoRespuesta.model_validate(self._comando(connection, id_vehiculo, identifier))

    def actualizar_comando(self, id_vehiculo: int, id_comando: int,
                          data: ComandoActualizar) -> ComandoRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self._comando(connection, id_vehiculo, id_comando, lock=True)
            self.repository.actualizar_comando(
                connection, id_comando, data.nombre, data.codigo, data.descripcion, data.activo,
            )
            return ComandoRespuesta.model_validate(self._comando(connection, id_vehiculo, id_comando))

    def eliminar_comando(self, id_vehiculo: int, id_comando: int) -> EliminacionRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self._comando(connection, id_vehiculo, id_comando, lock=True)
            self.repository.eliminar_comando(connection, id_comando)
            return EliminacionRespuesta(eliminado=True, id=id_comando)

    def crear_demo(self, id_vehiculo: int, data: DemoCrear) -> DemoRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            identifier = self.repository.crear_demo(connection, id_vehiculo, data.nombre, data.descripcion)
            return DemoRespuesta.model_validate(self._demo(connection, id_vehiculo, identifier))

    def actualizar_demo(self, id_vehiculo: int, id_demo: int, data: DemoActualizar) -> DemoRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self._demo(connection, id_vehiculo, id_demo)
            self.repository.actualizar_demo(connection, id_demo, data.nombre, data.descripcion, data.activo)
            return DemoRespuesta.model_validate(self._demo(connection, id_vehiculo, id_demo))

    def eliminar_demo(self, id_vehiculo: int, id_demo: int) -> EliminacionRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self._demo(connection, id_vehiculo, id_demo)
            self.repository.eliminar_demo(connection, id_demo)
            return EliminacionRespuesta(eliminado=True, id=id_demo)

    def crear_movimiento_demo(self, id_vehiculo: int, id_demo: int,
                              data: DemoMovimientoCrear) -> DemoMovimientoRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self._demo(connection, id_vehiculo, id_demo)
            self._comando(connection, id_vehiculo, data.id_comando, lock=True)
            self.repository.crear_movimiento_demo(
                connection, id_demo, data.id_comando, data.orden, data.duracion_ms, data.pausa_ms,
            )
            # El SP no retorna ID. (id_demo, orden) es UNIQUE en el contrato real.
            rows = self.repository.obtener_movimientos_demo(connection, id_demo)
            row = next((r for r in rows if r["orden"] == data.orden), None)
            if row is None:
                raise ApplicationError(500, "No se pudo confirmar el movimiento de DEMO.", "result_missing")
            return DemoMovimientoRespuesta.model_validate(row)

    def actualizar_movimiento_demo(self, id_vehiculo: int, id_demo: int,
                                   id_demo_movimiento: int,
                                   data: DemoMovimientoActualizar) -> DemoMovimientoRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self._demo(connection, id_vehiculo, id_demo)
            self._movimiento_demo(connection, id_demo, id_demo_movimiento)
            self._comando(connection, id_vehiculo, data.id_comando, lock=True)
            self.repository.actualizar_movimiento_demo(
                connection, id_demo_movimiento, data.id_comando,
                data.orden, data.duracion_ms, data.pausa_ms,
            )
            return DemoMovimientoRespuesta.model_validate(
                self._movimiento_demo(connection, id_demo, id_demo_movimiento),
            )

    def eliminar_movimiento_demo(self, id_vehiculo: int, id_demo: int,
                                 id_demo_movimiento: int) -> EliminacionRespuesta:
        with self.manager.transaction() as connection:
            self._vehiculo(connection, id_vehiculo, lock=True)
            self._demo(connection, id_vehiculo, id_demo)
            self._movimiento_demo(connection, id_demo, id_demo_movimiento)
            self.repository.eliminar_movimiento_demo(connection, id_demo_movimiento)
            return EliminacionRespuesta(eliminado=True, id=id_demo_movimiento)

    def ejecutar_movimiento(self, id_vehiculo: int, data: MovimientoEjecutar) -> MovimientoConfirmado:
        with self.manager.transaction() as connection:
            vehicle = self._vehiculo(connection, id_vehiculo, lock=True)
            if not vehicle["activo"]:
                raise ApplicationError(409, "El vehículo está inactivo.", "inactive_vehicle")
            command = self._comando(connection, id_vehiculo, data.id_comando, lock=True)
            if not command["activo"]:
                raise ApplicationError(409, "El comando está inactivo.", "inactive_command")
            # Lectura con bloqueo: conserva el valor actual incluso con conexiones concurrentes.
            previous = self.repository.bloquear_estado(connection, id_vehiculo)
            connected = bool(previous["conectado"]) if previous is not None else False
            self.repository.actualizar_estado(connection, id_vehiculo, data.id_comando, connected)
            history_id = self.repository.registrar_historial(
                connection, id_vehiculo, data.id_comando, data.origen,
            )
            state = self.repository.obtener_estado(connection, id_vehiculo)
            history_rows = self.repository.obtener_historial(connection, id_vehiculo)
            history = next((r for r in history_rows if r["id_historial"] == history_id), None)
            if state is None or history is None:
                raise ApplicationError(500, "No se pudo confirmar el movimiento.", "result_missing")
            result = MovimientoConfirmado(
                id_vehiculo=id_vehiculo, id_comando=data.id_comando, origen=data.origen,
                conectado=connected, estado=EstadoRespuesta.model_validate(state),
                historial=HistorialRespuesta.model_validate(history),
            )
        # CoordinatedVehiculoService publica este resultado DESPUÉS del commit,
        # sin deshacer ni repetir una transacción si un monitor se desconecta.
        return result
