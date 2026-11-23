"""Administración, catálogo comercial, reservas y edición de DEMOS."""
from collections import deque
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from app.errors import ApplicationError
from app.platform43 import auth, commercial as data
from app.platform43.commercial_models import (
    AssignmentInput, AuditView, ClientInput, ClientStatus, ClientView, CommercialState, DashboardView,
    DemoInput, DemoView, InventoryUnitView, ModelInput, ModelView, OwnershipView, PublicModelView,
    ReservationInput, ReservationIssued, ReservationView, UnitInput, UnitView,
)


router = APIRouter(prefix="/plataforma", dependencies=[Depends(auth.require_enabled)],
                   tags=["Plataforma 4.3 • Administración"])
Admin = Annotated[auth.Actor, Depends(auth.require_admin)]
ResourceID = Annotated[int, Path(ge=1, le=2**53 - 1)]
Search = Annotated[str, Query(max_length=100)]
OptionalID = Annotated[int | None, Query(ge=1, le=2**53 - 1)]


class ReservationQuota:
    """Limita intentos públicos por cliente y por proceso antes de acceder a RDS."""
    def __init__(self):
        self._entries = deque()
        self._lock = Lock()

    def require(self, client):
        with self._lock:
            now = monotonic()
            while self._entries and self._entries[0][0] <= now - 60:
                self._entries.popleft()
            if len(self._entries) >= 120 or sum(value == client for _, value in self._entries) >= 10:
                raise ApplicationError(429, "Demasiados intentos de reserva. Intenta más tarde.", "reservation_quota")
            self._entries.append((now, client))


reservation_quota = ReservationQuota()


@router.get("/admin/dashboard", response_model=DashboardView)
def dashboard(actor: Admin):
    with data.store.manager.transaction(readonly=True) as db:
        return data.dashboard(db)


@router.get("/admin/modelos", response_model=list[ModelView])
def models(actor: Admin, q: Search = ""):
    with data.store.manager.transaction(readonly=True) as db:
        return data.models(db, q)


@router.post("/admin/modelos", status_code=201, response_model=ModelView)
def create_model(payload: ModelInput, actor: Admin):
    with data.store.manager.transaction() as db:
        value = data.save_model(db, payload)
        auth.audit(db, actor.id, "alta", "modelo", value["id"])
        return value


@router.put("/admin/modelos/{model_id}", response_model=ModelView)
def update_model(model_id: ResourceID, payload: ModelInput, actor: Admin):
    with data.store.manager.transaction() as db:
        value = data.save_model(db, payload, model_id)
        auth.audit(db, actor.id, "edicion", "modelo", model_id)
        return value


@router.get("/admin/unidades", response_model=list[InventoryUnitView])
def units(actor: Admin, q: Search = "", estado: CommercialState | None = None):
    with data.store.manager.transaction(readonly=True) as db:
        return data.units(db, q, estado)


@router.post("/admin/unidades", status_code=201, response_model=InventoryUnitView)
def create_unit(payload: UnitInput, actor: Admin):
    with data.store.manager.transaction() as db:
        value = data.save_unit(db, payload)
        auth.audit(db, actor.id, "alta", "unidad", value["id"])
        return value


@router.put("/admin/unidades/{unit_id}", response_model=InventoryUnitView)
def update_unit(unit_id: ResourceID, payload: UnitInput, actor: Admin):
    with data.store.manager.transaction() as db:
        value = data.save_unit(db, payload, unit_id)
        auth.audit(db, actor.id, "edicion", "unidad", unit_id)
        return value


@router.post("/admin/unidades/{unit_id}/asignacion", response_model=InventoryUnitView)
def assign(unit_id: ResourceID, payload: AssignmentInput, actor: Admin):
    with data.store.manager.transaction() as db:
        value = data.assign(db, actor, unit_id, payload)
        action = "asignacion" if payload.id_cliente is not None else "retiro_asignacion"
        auth.audit(db, actor.id, action, "unidad", unit_id)
        return value


@router.get("/admin/unidades/{unit_id}/titularidad", response_model=list[OwnershipView])
def ownership(unit_id: ResourceID, actor: Admin):
    with data.store.manager.transaction(readonly=True) as db:
        return data.title_history(db, unit_id)


@router.get("/admin/clientes", response_model=list[ClientView])
def clients(actor: Admin, q: Search = ""):
    with data.store.manager.transaction(readonly=True) as db:
        return data.clients(db, q)


@router.post("/admin/clientes", status_code=201, response_model=ClientView)
def create_client(payload: ClientInput, actor: Admin):
    password_hash = auth.hash_password(payload.contrasena)
    with data.store.manager.transaction() as db:
        value = data.add_client(db, payload, password_hash)
        auth.audit(db, actor.id, "alta", "cliente", value["id"])
        return value


@router.patch("/admin/clientes/{client_id}", response_model=ClientView)
def change_client(client_id: ResourceID, payload: ClientStatus, actor: Admin):
    with data.store.manager.transaction() as db:
        value = data.set_client_status(db, client_id, payload.activo)
        auth.audit(db, actor.id, "habilitacion" if payload.activo else "bloqueo", "cliente", client_id)
        return value


@router.get("/admin/reservas", response_model=list[ReservationView])
def reservations(actor: Admin):
    with data.store.manager.transaction(readonly=True) as db:
        return data.reservations(db)


@router.post("/admin/reservas/{reservation_id}/revocar")
def revoke(reservation_id: ResourceID, actor: Admin):
    with data.store.manager.transaction() as db:
        value = data.revoke_reservation(db, reservation_id)
        auth.audit(db, actor.id, "revocacion", "reserva", reservation_id)
        return value


@router.get("/admin/auditoria", response_model=list[AuditView])
def audit_records(actor: Admin):
    with data.store.manager.transaction(readonly=True) as db:
        return data.audit_records(db)


@router.get("/mis-unidades", response_model=list[UnitView])
def personal_units(actor: auth.ActorDep):
    if actor.rol != "comprador":
        auth.note_denial(actor, "mis_unidades", None)
        raise ApplicationError(403, "Este listado corresponde a compradores.", "platform_forbidden")
    with data.store.manager.transaction(readonly=True) as db:
        return data.my_units(db, actor)


@router.get("/catalogo", response_model=list[PublicModelView])
def catalog():
    with data.store.manager.transaction(readonly=True) as db:
        return data.public_catalog(db)


@router.post("/reservas", status_code=201, response_model=ReservationIssued)
def reserve(payload: ReservationInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    reservation_quota.require(request.client.host if request.client else "")
    try:
        with data.store.manager.transaction() as db:
            value = data.reserve(db, payload.id_unidad)
            auth.audit(db, None, "inicio", "reserva", value["id_reserva"])
            return value
    except ApplicationError as error:
        if error.status_code in (404, 409):
            auth.note_denial(None, "reserva", payload.id_unidad)
        raise


@router.delete("/reservas/actual", status_code=204)
def finish_reservation(actor: auth.ActorDep):
    if actor.rol != "invitado":
        auth.note_denial(actor, "reserva", None)
        raise ApplicationError(403, "No hay una reserva de invitado en esta sesión.", "platform_forbidden")
    with data.store.manager.transaction() as db:
        data.revoke_reservation(db, actor.reservation_id)
        auth.audit(db, None, "fin", "reserva", actor.reservation_id)
    return Response(status_code=204)


@router.get("/demos", response_model=list[DemoView])
def list_demos(actor: auth.ActorDep, id_modelo: OptionalID = None, id_unidad: OptionalID = None):
    try:
        with data.store.manager.transaction(readonly=True) as db:
            return data.list_demos(db, actor, id_modelo, id_unidad)
    except ApplicationError as error:
        if error.status_code in (403, 404):
            auth.note_denial(actor, "demo", id_unidad or id_modelo)
        raise


@router.post("/demos", status_code=201, response_model=DemoView)
def create_demo(payload: DemoInput, actor: auth.ActorDep):
    return _write_demo(payload, actor)


@router.put("/demos/{demo_id}", response_model=DemoView)
def update_demo(demo_id: ResourceID, payload: DemoInput, actor: auth.ActorDep):
    return _write_demo(payload, actor, demo_id)


def _write_demo(payload, actor, demo_id=None):
    try:
        with data.store.manager.transaction() as db:
            value = data.save_demo(db, actor, payload, demo_id)
            auth.audit(db, actor.id, "edicion" if demo_id else "alta", "demo", value["id"])
            return value
    except ApplicationError as error:
        if error.status_code in (403, 404):
            auth.note_denial(actor, "demo", demo_id)
        raise


@router.delete("/demos/{demo_id}", status_code=204)
def disable_demo(demo_id: ResourceID, actor: auth.ActorDep):
    try:
        with data.store.manager.transaction() as db:
            data.disable_demo(db, actor, demo_id)
            auth.audit(db, actor.id, "baja", "demo", demo_id)
    except ApplicationError as error:
        if error.status_code in (403, 404):
            auth.note_denial(actor, "demo", demo_id)
        raise
    return Response(status_code=204)
