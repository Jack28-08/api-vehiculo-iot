"""Comprobaciones independientes de la API y de su acceso a MySQL."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database.connection import DatabaseUnavailableError, MySQLConnectionManager
from app.models.health import DatabaseUnavailableResponse, HealthResponse

router = APIRouter(tags=["Salud"])


@router.get("/health", response_model=HealthResponse, summary="Estado de la API")
def health() -> HealthResponse:
    """Confirma que la API responde; no verifica MySQL ni Amazon RDS."""
    return HealthResponse()


@router.get(
    "/health/db",
    response_model=HealthResponse,
    summary="Estado de la conexión con MySQL",
    responses={
        503: {
            "model": DatabaseUnavailableResponse,
            "description": "La configuración o la conexión de MySQL no está disponible.",
        }
    },
)
def health_db() -> HealthResponse | JSONResponse:
    """Ejecuta la comprobación síncrona en el grupo de hilos de FastAPI."""
    try:
        MySQLConnectionManager().check_connection()
    except DatabaseUnavailableError:
        return JSONResponse(
            status_code=503,
            content=DatabaseUnavailableResponse().model_dump(),
        )
    return HealthResponse()
