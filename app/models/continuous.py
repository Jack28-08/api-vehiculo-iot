"""Contratos de control continuo, independientes de los comandos de RDS."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


Sequence = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
Axis = Annotated[int, Field(strict=True, ge=-1, le=1)]


class Gesture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    barrera: UUID
    gesto: UUID


class ControlVector(Gesture):
    secuencia: Sequence
    longitudinal: Axis
    lateral: Axis


class Capability(BaseModel):
    habilitado: bool
    disponible: bool
    diagonales: bool
    demo_remota: bool
    barrera: UUID | None
    ttl_ms: int
    renovar_ms: int


class ControlGrant(Gesture):
    secuencia: Sequence
    ttl_ms: int
    id_operacion: UUID | None = None
    id_historial: int | None = None
    ms_persistencia: int | None = None
    tiempos_ms: dict[str, int] | None = None


class ReleaseResult(BaseModel):
    liberado: Literal[True] = True
    barrera: UUID
    id_operacion: UUID | None = None


class OutputAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secuencia: Sequence
    estado: Literal["aplicado", "liberado"]
    mascara: Annotated[int, Field(strict=True, ge=0, le=255)]
    simulado: Annotated[bool, Field(strict=True)]


class DeviceHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["latido"]
    reto: Annotated[int, Field(strict=True, ge=1, le=2**32 - 1)]
    acuse: OutputAck | None = None


class DeviceSnapshot(BaseModel):
    tipo: Literal["control"] = "control"
    reto: int
    secuencia: int
    vigencia_ms: int
    longitudinal: Axis = 0
    lateral: Axis = 0
    codigo: int = 0
    pulso: int = 0
