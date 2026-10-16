"""Protocolo de monitoreo; ningún evento acredita actuación física del vehículo."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

EventType = Literal[
    "estado_inicial", "movimiento_individual", "demo_inicio", "demo_movimiento",
    "demo_fin", "demo_cancelada", "demo_error", "detencion_logica",
]


class VehicleEvent(BaseModel):
    tipo: EventType
    id_vehiculo: int
    id_comando: int | None = None
    comando: str | None = None
    codigo: int | None = None
    fecha_hora: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    conectado: bool | None = None
    id_ejecucion: str | None = None
    id_demo: int | None = None
    total_movimientos: int | None = None
    orden: int | None = None
    duracion_ms: int | None = None
    pausa_ms: int | None = None
    mensaje: str | None = None

    @field_validator("fecha_hora")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        # Las conexiones MySQL de esta API establecen su sesión en +00:00.
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
                else value.astimezone(timezone.utc))


def state_event(tipo: EventType, id_vehiculo: int, state: dict | None, **metadata) -> VehicleEvent:
    values = dict(state or {})
    timestamp = values.get("fecha_actualizacion")
    return VehicleEvent(
        tipo=tipo, id_vehiculo=id_vehiculo, id_comando=values.get("id_comando"),
        comando=values.get("comando"), codigo=values.get("codigo"),
        conectado=values.get("conectado"),
        **({"fecha_hora": timestamp} if timestamp is not None else {}), **metadata,
    )
