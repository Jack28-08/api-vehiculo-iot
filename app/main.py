"""Punto de entrada de la aplicación ASGI."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.database.connection import DatabaseUnavailableError
from app.errors import ApplicationError
from app.routes.health import router as health_router
from app.routes.vehiculos import router as vehicles_router
from app.routes.demos import router as demos_router
from app.routes.websocket import router as websocket_router
from app.runtime import runtime


@asynccontextmanager
async def lifespan(application):
    runtime.bind_loop()
    application.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.shutdown()

app = FastAPI(
    title="API Vehículo IoT",
    description=(
        "Práctica 2.5: HTTP con POO y los 21 SP del contrato real de RDS. "
        "GET y WebSocket de monitoreo públicos; POST con X-API-Key. Operador: movimientos y DEMO; "
        "administrador: gestión y movimientos. Las actualizaciones y eliminaciones "
        "usan POST por requisito de la práctica. DEMO y DETENER son operaciones lógicas; "
        "ningún evento confirma actuación física del microcontrolador."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(vehicles_router)
app.include_router(demos_router)
app.include_router(websocket_router)


@app.exception_handler(ApplicationError)
async def application_error_handler(request, error: ApplicationError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"detail": error.message})


@app.exception_handler(DatabaseUnavailableError)
async def database_error_handler(request, error: DatabaseUnavailableError) -> JSONResponse:
    if error.code == "conflict":
        code, message = 409, "La operación entra en conflicto con datos duplicados o relacionados."
    elif error.code == "validation":
        code, message = 422, "Los datos no son válidos para esta operación."
    elif error.code in {"query", "permission"}:
        code, message = 500, "No se pudo completar la operación."
    else:
        code, message = 503, "Base de datos no disponible."
    return JSONResponse(status_code=code, content={"detail": message})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, error: RequestValidationError) -> JSONResponse:
    # Pydantic incluye el valor recibido en sus errores por defecto. No lo reflejamos.
    details = [{"loc": e["loc"], "type": e["type"], "msg": "Valor inválido."}
               for e in error.errors()]
    return JSONResponse(status_code=422, content={"detail": details})


@app.exception_handler(Exception)
async def unexpected_error_handler(request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "Error interno de la API."})
