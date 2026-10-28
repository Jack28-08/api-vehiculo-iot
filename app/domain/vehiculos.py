"""Entidades inmutables que deciden actividad y pertenencia de recursos.

Los modelos de ``app.models`` validan el contrato HTTP. Estas entidades se
construyen a partir de las filas ya consultadas y contienen reglas del negocio.
No abren conexiones, no conocen FastAPI y no representan motores físicos.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.errors import ApplicationError


@dataclass(frozen=True, slots=True)
class Vehiculo:
    id_vehiculo: int
    nombre: str
    activo: bool

    @classmethod
    def desde_fila(cls, fila: Mapping[str, Any]) -> "Vehiculo":
        return cls(fila["id_vehiculo"], fila["nombre"], bool(fila["activo"]))

    def exigir_activo(self) -> None:
        if not self.activo:
            raise ApplicationError(409, "El vehículo está inactivo.", "inactive_vehicle")


@dataclass(frozen=True, slots=True)
class Comando:
    id_comando: int
    id_vehiculo: int
    nombre: str
    codigo: int
    activo: bool

    @classmethod
    def desde_fila(cls, fila: Mapping[str, Any]) -> "Comando":
        return cls(fila["id_comando"], fila["id_vehiculo"], fila["nombre"],
                   fila["codigo"], bool(fila["activo"]))

    def exigir_pertenencia(self, vehiculo: Vehiculo) -> None:
        if self.id_vehiculo != vehiculo.id_vehiculo:
            raise ApplicationError(404, "Comando no encontrado en este vehículo.", "command_not_found")

    def exigir_activo(self) -> None:
        if not self.activo:
            raise ApplicationError(409, "El comando está inactivo.", "inactive_command")


@dataclass(frozen=True, slots=True)
class Demo:
    id_demo: int
    id_vehiculo: int
    nombre: str
    activo: bool

    @classmethod
    def desde_fila(cls, fila: Mapping[str, Any]) -> "Demo":
        return cls(fila["id_demo"], fila["id_vehiculo"], fila["nombre"], bool(fila["activo"]))

    def exigir_pertenencia(self, vehiculo: Vehiculo) -> None:
        if self.id_vehiculo != vehiculo.id_vehiculo:
            raise ApplicationError(404, "DEMO no encontrada en este vehículo.", "demo_not_found")

    def exigir_activa(self) -> None:
        if not self.activo:
            raise ApplicationError(409, "La DEMO está inactiva.", "inactive_resource")
