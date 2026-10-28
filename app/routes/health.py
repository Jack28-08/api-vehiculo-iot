"""Comprobaciones independientes de la API y de su acceso a MySQL."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database.connection import DatabaseUnavailableError, MySQLConnectionManager
from app.models.health import DatabaseUnavailableResponse, HealthResponse


class HealthController:
    """Controlador instanciado: sus métodos enlazados atienden las rutas."""

    def __init__(self) -> None:
        self._router = APIRouter(tags=["Salud"])
        self._router.add_api_route(
            "/health", self.health, methods=["GET"],
            response_model=HealthResponse,
            summary="Estado de la API",
        )
        self._router.add_api_route(
            "/health/db", self.health_db, methods=["GET"],
            response_model=HealthResponse,
            summary="Estado de la conexión con MySQL",
            responses={
                503: {
                    "model": DatabaseUnavailableResponse,
                    "description": "La configuración o la conexión de MySQL no está disponible.",
                }
            },
        )

    @property
    def router(self) -> APIRouter:
        return self._router

    def health(self) -> HealthResponse:
        """Confirma que la API responde; no verifica MySQL ni Amazon RDS."""
        return HealthResponse()

    def health_db(self) -> HealthResponse | JSONResponse:
        """Ejecuta la comprobación síncrona en el grupo de hilos de FastAPI."""
        try:
            MySQLConnectionManager().check_connection()
        except DatabaseUnavailableError:
            return JSONResponse(
                status_code=503,
                content=DatabaseUnavailableResponse().model_dump(),
            )
        return HealthResponse()


controller = HealthController()
router = controller.router

# Compatibilidad para las comprobaciones que importan estos métodos.
health = controller.health
health_db = controller.health_db
