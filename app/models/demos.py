"""Respuestas de control lógico de ejecuciones, sin confirmación del hardware."""

from typing import Literal

from pydantic import BaseModel


class DemoExecutionResponse(BaseModel):
    id_ejecucion: str
    id_vehiculo: int
    id_demo: int
    total_movimientos: int
    estado: Literal["iniciada", "finalizada", "cancelada", "error"]


class LogicalStopResponse(BaseModel):
    id_vehiculo: int
    id_ejecucion: str | None
    detencion_logica: Literal[True]
    estado: Literal["detenida_logicamente"]
