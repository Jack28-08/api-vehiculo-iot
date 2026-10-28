"""Polimorfismo real de las escrituras de movimiento.

El método plantilla ``Movimiento.ejecutar`` comparte el algoritmo de estado.
Las subclases deciden si hay registro de historial. El servicio que invoca la
operación es dueño de la transacción: estas clases nunca hacen commit ni
publican eventos, para conservar la atomicidad y el orden de notificación.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from app.domain import Comando, Vehiculo
from app.errors import ApplicationError


class RepositorioMovimientos(Protocol):
    """Operaciones que necesita un movimiento, sin depender del conector SQL."""

    def bloquear_estado(self, connection: Any, id_vehiculo: int) -> dict | None: ...
    def actualizar_estado(self, connection: Any, id_vehiculo: int,
                          id_comando: int, conectado: bool) -> None: ...
    def registrar_historial(self, connection: Any, id_vehiculo: int,
                            id_comando: int, origen: str) -> int: ...
    def obtener_estado(self, connection: Any, id_vehiculo: int) -> dict | None: ...
    def obtener_historial(self, connection: Any, id_vehiculo: int) -> list[dict]: ...


@dataclass(frozen=True, slots=True)
class ResultadoMovimiento:
    conectado: bool
    estado: dict
    historial: dict | None


class Movimiento(ABC):
    """Abstracción de un movimiento lógico con recursos de dominio propios."""

    def __init__(self, vehiculo: Vehiculo, comando: Comando) -> None:
        self._vehiculo = vehiculo
        self._comando = comando

    @property
    def vehiculo(self) -> Vehiculo:
        return self._vehiculo

    @property
    def comando(self) -> Comando:
        return self._comando

    def ejecutar(self, repository: RepositorioMovimientos,
                 connection: Any) -> ResultadoMovimiento:
        self.vehiculo.exigir_activo()
        self.comando.exigir_pertenencia(self.vehiculo)
        self.comando.exigir_activo()
        previous = repository.bloquear_estado(connection, self.vehiculo.id_vehiculo)
        connected = bool(previous["conectado"]) if previous is not None else False
        repository.actualizar_estado(connection, self.vehiculo.id_vehiculo,
                                     self.comando.id_comando, connected)
        history_id = self._registrar_historial(repository, connection)
        state = repository.obtener_estado(connection, self.vehiculo.id_vehiculo)
        history = None
        if history_id is not None:
            rows = repository.obtener_historial(connection, self.vehiculo.id_vehiculo)
            history = next((row for row in rows if row["id_historial"] == history_id), None)
        if state is None or (history_id is not None and history is None):
            raise ApplicationError(500, "No se pudo confirmar el movimiento.", "result_missing")
        return ResultadoMovimiento(connected, state, history)

    @abstractmethod
    def _registrar_historial(self, repository: RepositorioMovimientos,
                            connection: Any) -> int | None:
        """Cada tipo de movimiento determina su política de historial."""
        raise NotImplementedError


class MovimientoIndividual(Movimiento):
    def __init__(self, vehiculo: Vehiculo, comando: Comando, origen: str) -> None:
        super().__init__(vehiculo, comando)
        self._origen = origen

    @property
    def origen(self) -> str:
        return self._origen

    def _registrar_historial(self, repository: RepositorioMovimientos,
                            connection: Any) -> int:
        return repository.registrar_historial(
            connection, self.vehiculo.id_vehiculo, self.comando.id_comando, self.origen,
        )


class MovimientoDeDemo(Movimiento):
    def _registrar_historial(self, repository: RepositorioMovimientos,
                            connection: Any) -> None:
        # Requisito del examen: cada paso cambia estado y nunca escribe historial.
        return None
