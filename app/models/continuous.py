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
    barrera: UUID | None
    ttl_ms: int
    renovar_ms: int


class ControlGrant(Gesture):
    secuencia: Sequence
    ttl_ms: int


class ReleaseResult(BaseModel):
    liberado: Literal[True] = True
    barrera: UUID


class DeviceHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["latido"]
    reto: Annotated[int, Field(strict=True, ge=1, le=2**32 - 1)]


class DeviceSnapshot(BaseModel):
    tipo: Literal["control"] = "control"
    reto: int
    secuencia: int
    vigencia_ms: int
    longitudinal: Axis = 0
    lateral: Axis = 0
