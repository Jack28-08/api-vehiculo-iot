"""Ejecuta secuencias comerciales con el coordinador de DEMO de NODE/01."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from app.errors import ApplicationError
from app.models.demos import DemoExecutionResponse
from app.platform43 import auth, commercial
from app.platform43.access import _with_unit, command_policy
from app.routes.vehiculos import ControlBarrier
from app.runtime import runtime
from app.services.demo_data import DemoPlan


router = APIRouter(prefix="/plataforma", tags=["Plataforma 4.3 • Ejecución"])


class DemoRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id_unidad: int = Field(strict=True, gt=0)


def _start(actor, demo_id, unit_id, barrier):
    if actor.rol != "comprador":
        raise ApplicationError(403, "La ejecución requiere titularidad.", "platform_demo")
    with auth.manager.transaction() as connection:
        active = auth.active_actor(connection, session_id=actor.session_id)
        compiled = commercial.get_demo_plan(connection, active, demo_id, unit_id)
    vehicle_id = compiled["id_vehiculo"]
    scope = _with_unit(actor, vehicle_id)
    if scope.unit_id != unit_id or scope.version != compiled["version_unidad"]:
        raise ApplicationError(403, "Unidad no autorizada.", "platform_demo_unit")
    if scope.fisico:
        capability = runtime.continuous.capability(vehicle_id)
        if not capability.habilitado or not capability.disponible:
            raise ApplicationError(503, "El canal protegido del equipo no está disponible.", "platform_device")
    plan = DemoPlan(vehicle_id, demo_id, tuple(compiled["pasos"]), commercial=True)
    return runtime.demos.start(vehicle_id, demo_id, barrier=barrier, policy=command_policy(scope), prepared=plan)


@router.post("/demos/{demo_id}/ejecutar", status_code=202, response_model=DemoExecutionResponse)
async def execute_demo(demo_id: Annotated[int, Path(gt=0)], payload: DemoRun, actor: auth.ActorDep,
                       barrier: ControlBarrier = None):
    task = runtime.track_control(asyncio.create_task(asyncio.to_thread(_start, actor, demo_id, payload.id_unidad, barrier)))
    return await asyncio.shield(task)
