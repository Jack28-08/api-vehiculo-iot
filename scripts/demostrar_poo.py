"""Demostración local de las mismas clases de movimiento que utiliza la API.

Ejecutar: python -m scripts.demostrar_poo
No carga credenciales, no abre sockets y no se conecta a Amazon RDS.
El repositorio en memoria es un adaptador de demostración, no persistencia real.
"""

import json

from app.domain import Comando, Vehiculo
from app.services.movimientos import Movimiento, MovimientoDeDemo, MovimientoIndividual


class RepositorioMemoria:
    """Adaptador del contrato RepositorioMovimientos para una ejecución local."""

    def __init__(self) -> None:
        self._estado: dict | None = None
        self._historial: list[dict] = []

    def bloquear_estado(self, connection, id_vehiculo):
        return self.obtener_estado(connection, id_vehiculo)

    def actualizar_estado(self, connection, id_vehiculo, id_comando, conectado):
        self._estado = {"id_vehiculo": id_vehiculo, "id_comando": id_comando,
                        "conectado": conectado}

    def registrar_historial(self, connection, id_vehiculo, id_comando, origen):
        identifier = len(self._historial) + 1
        self._historial.append({"id_historial": identifier, "id_vehiculo": id_vehiculo,
                                "id_comando": id_comando, "origen": origen})
        return identifier

    def obtener_estado(self, connection, id_vehiculo):
        if self._estado is not None and self._estado["id_vehiculo"] == id_vehiculo:
            return dict(self._estado)
        return None

    def obtener_historial(self, connection, id_vehiculo):
        return [dict(row) for row in self._historial if row["id_vehiculo"] == id_vehiculo]


def demostrar() -> dict:
    vehicle = Vehiculo(1, "Vehículo de demostración local", True)
    command = Comando(2, 1, "AVANZAR", 2, True)
    repository = RepositorioMemoria()
    # Ambas implementaciones se utilizan mediante la abstracción Movimiento.
    movements: tuple[Movimiento, ...] = (
        MovimientoIndividual(vehicle, command, "demostracion_local"),
        MovimientoDeDemo(vehicle, command),
    )
    results = []
    for movement in movements:
        result = movement.ejecutar(repository, connection=None)
        results.append({"clase": type(movement).__name__,
                        "estado": result.estado,
                        "registro_historial": result.historial is not None,
                        "total_historial": len(repository.obtener_historial(None, 1))})
    if [row["registro_historial"] for row in results] != [True, False]:
        raise RuntimeError("La política de historial de movimientos no se cumple.")
    return {"modo": "local_sin_rds", "ejecuciones": results,
            "historial_total": len(repository.obtener_historial(None, 1))}


if __name__ == "__main__":
    print(json.dumps(demostrar(), ensure_ascii=False, indent=2))
