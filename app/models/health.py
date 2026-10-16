"""Contratos públicos de respuesta para las comprobaciones de salud."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Indica que la comprobación solicitada terminó correctamente."""

    status: Literal["ok"] = "ok"


class DatabaseUnavailableResponse(BaseModel):
    """Respuesta estable que no expone errores internos ni credenciales."""

    status: Literal["unavailable"] = "unavailable"
    message: Literal["Base de datos no disponible."] = "Base de datos no disponible."
