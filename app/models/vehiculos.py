"""Contratos HTTP derivados del catálogo real, incluidos los SELECT con JOIN.

Los modelos de entrada validan los límites de los parámetros de los SP. Una
actualización representa los valores completos que recibirá el procedimiento;
los campos opcionales omitidos se escriben como NULL.
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints


INT_MAX = 2_147_483_647
INT_MIN = -2_147_483_648
Identificador = Annotated[StrictInt, Field(ge=1, le=INT_MAX)]
EnteroSQL = Annotated[StrictInt, Field(ge=INT_MIN, le=INT_MAX)]
Nombre80 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Nombre60 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Texto80 = Annotated[str, StringConstraints(max_length=80)]
Texto50 = Annotated[str, StringConstraints(max_length=50)]
Descripcion = Annotated[str, StringConstraints(max_length=255)]
Origen = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]


class Entrada(BaseModel):
    """Rechaza propiedades desconocidas y errores habituales de tipo."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class VehiculoCrear(Entrada):
    """Valores admitidos por sp_crear_vehiculo; el SP crea activo=true."""

    nombre: Nombre80
    modelo: Texto80 | None = None
    descripcion: Descripcion | None = None
    microcontrolador: Texto50 | None = None


class VehiculoActualizar(VehiculoCrear):
    """Reemplaza los campos editables; no modifica id ni fecha_registro."""

    activo: StrictBool


class VehiculoRespuesta(BaseModel):
    """SELECT * de vehiculos, con las columnas y nulabilidad reales."""

    id_vehiculo: int
    nombre: str
    modelo: str | None
    descripcion: str | None
    microcontrolador: str | None
    fecha_registro: datetime | None
    activo: bool


class ComandoCrear(Entrada):
    """Comando de catálogo; codigo es INT firmado, sin enum inventado."""

    nombre: Nombre60
    codigo: EnteroSQL
    descripcion: Descripcion | None = None


class ComandoActualizar(ComandoCrear):
    activo: StrictBool


class ComandoRespuesta(BaseModel):
    id_comando: int
    id_vehiculo: int
    nombre: str
    codigo: int
    descripcion: str | None
    activo: bool


class DemoCrear(Entrada):
    nombre: Nombre80
    descripcion: Descripcion | None = None


class DemoActualizar(DemoCrear):
    activo: StrictBool


class DemoRespuesta(BaseModel):
    id_demo: int
    id_vehiculo: int
    nombre: str
    descripcion: str | None
    activo: bool


class DemoMovimientoCrear(Entrada):
    """Los SP reciben INT firmado aunque los tiempos se guarden unsigned."""

    id_comando: Identificador
    orden: Annotated[StrictInt, Field(ge=1, le=INT_MAX)]
    duracion_ms: Annotated[StrictInt, Field(ge=1, le=INT_MAX)]
    pausa_ms: Annotated[StrictInt, Field(ge=0, le=INT_MAX)] = 0


class DemoMovimientoActualizar(DemoMovimientoCrear):
    """Reemplaza un movimiento de una DEMO ya perteneciente al vehículo."""


class DemoMovimientoRespuesta(BaseModel):
    """Columnas del JOIN de sp_obtener_movimientos_demo; no incluye id_demo."""

    id_demo_movimiento: int
    orden: int
    id_comando: int
    comando: str
    codigo: int
    duracion_ms: int
    pausa_ms: int


class EstadoRespuesta(BaseModel):
    """JOIN de estado, vehículo y comando; no devuelve id_estado."""

    id_vehiculo: int
    vehiculo: str
    id_comando: int | None
    comando: str | None
    codigo: int | None
    conectado: bool
    fecha_actualizacion: datetime | None


class HistorialRespuesta(BaseModel):
    """JOIN de historial y comando; el SP no devuelve id_comando."""

    id_historial: int
    id_vehiculo: int
    comando: str | None
    codigo: int | None
    fecha_hora: datetime | None
    origen: str


class MovimientoEjecutar(Entrada):
    """Ejecuta un comando existente; origen no presume conexión física."""

    id_comando: Identificador
    origen: Origen


class MovimientoConfirmado(BaseModel):
    """Resultado del estado e historial cuya transacción ya fue confirmada."""

    id_vehiculo: int
    id_comando: int
    origen: str
    conectado: bool
    estado: EstadoRespuesta
    historial: HistorialRespuesta


class EliminacionRespuesta(BaseModel):
    eliminado: bool
    id: int
