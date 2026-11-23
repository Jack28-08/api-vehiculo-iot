"""Autorización del vehículo y tickets del monitor cuando se habilita 4.3."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import secrets

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.database.connection import DatabaseUnavailableError
from app.errors import ApplicationError
from app.platform43 import auth
from app.runtime import runtime
from app.services.continuous import DIRECTION_CODES, VEHICLE_ID as DEVICE_VEHICLE_ID


router = APIRouter(prefix="/plataforma", tags=["Plataforma 4.3 • Monitoreo"])
_VEHICLE_PATH = re.compile(r"/vehiculos/([1-9][0-9]{0,9})(?:/(.*))?\Z")
_DEMO_READ = re.compile(r"demos(?:/[1-9][0-9]{0,9}/movimientos)?\Z")
_DEMO_RUN = re.compile(r"demos/([1-9][0-9]{0,9})/(iniciar|cancelar)\Z")
_CONTROL_ACTIONS = {"control/permiso", "control/vector", "control/liberar"}
_READ_ACTIONS = {"", "comandos", "estado", "historial", "control/capacidad"}
_ORIGINS = {"https://jack28-08.github.io", "http://localhost:5500"}


@dataclass(frozen=True)
class Scope:
    actor_id: int | None
    session_id: int
    unit_id: int
    vehicle_id: int
    version: int
    fisico: bool
    guest: bool
    history_since: datetime | None = None


def _forbidden(message="La unidad no está autorizada para esta sesión."):
    return ApplicationError(403, message, "platform_vehicle")


def unit_scope(connection, actor, vehicle_id, *, locking=False):
    ending = " FOR SHARE OF u" if locking else ""
    unit = auth.one(connection, """SELECT u.id, u.id_vehiculo, u.id_modelo, u.id_cliente, u.estado,
            u.fisico, u.version, m.publicado, t.inicio AS desde
            FROM iot43_unidades AS u JOIN iot43_modelos AS m ON m.id=u.id_modelo
            JOIN vehiculos AS v ON v.id_vehiculo=u.id_vehiculo
            LEFT JOIN iot43_titularidad AS t ON t.id=u.id_titularidad AND t.fin IS NULL
                AND t.id_unidad=u.id AND t.id_cliente=u.id_cliente
            WHERE u.id_vehiculo=%s AND m.activo=1 AND v.activo=1""" + ending, (vehicle_id,))
    if unit is None:
        raise ApplicationError(404, "Unidad no disponible.", "platform_unit")
    guest = actor.rol == "invitado"
    buyer = (actor.rol == "comprador" and unit["estado"] == "vendida"
             and unit["id_cliente"] == actor.id and unit["desde"] is not None)
    if buyer:
        return unit
    if guest and unit["estado"] == "exhibicion" and unit["publicado"] and not unit["fisico"] and vehicle_id != DEVICE_VEHICLE_ID:
        reservation = auth.one(connection, """SELECT id FROM iot43_reservas
            WHERE id=%s AND id_unidad=%s AND ended_at IS NULL AND expires_at>UTC_TIMESTAMP(6)"""
            + (" FOR SHARE" if locking else ""),
            (actor.reservation_id, unit["id"]))
        if reservation:
            return unit
    raise _forbidden()


def checked_scope(connection, scope, *, locking=False):
    actor = auth.active_actor(connection, session_id=scope.session_id, locking=locking)
    unit = unit_scope(connection, actor, scope.vehicle_id, locking=locking)
    if unit["id"] != scope.unit_id or unit["version"] != scope.version:
        raise _forbidden("La asignación de la unidad cambió.")
    return actor, unit


def _with_unit(actor, vehicle_id):
    with auth.manager.transaction(readonly=True) as connection:
        unit = unit_scope(connection, actor, vehicle_id)
    return Scope(actor.id, actor.session_id, unit["id"], vehicle_id, unit["version"],
                 bool(unit["fisico"]) or vehicle_id == DEVICE_VEHICLE_ID, actor.rol == "invitado", unit["desde"])


def _capability(connection, actor, unit, code):
    allowed = auth.one(connection, """SELECT control, invitado FROM iot43_capacidades
            WHERE id_modelo=%s AND codigo=%s FOR SHARE""", (unit["id_modelo"], code))
    if not allowed or not allowed["control"] or (actor.rol == "invitado" and not allowed["invitado"]):
        raise _forbidden("El modelo no admite este comando para tu sesión.")


def _verify_locked(connection, scope, code, command_id):
    # Espera confirmaciones pendientes de titularidad o cuenta. En una escritura
    # se conserva el bloqueo compartido hasta el commit del movimiento.
    if scope.actor_id is not None:
        auth.one(connection, "SELECT id FROM iot43_cuentas WHERE id=%s FOR SHARE", (scope.actor_id,))
    auth.one(connection, "SELECT id FROM iot43_unidades WHERE id=%s FOR SHARE", (scope.unit_id,))
    auth.one(connection, "SELECT id FROM iot43_sesiones WHERE id=%s FOR SHARE", (scope.session_id,))
    actor, unit = checked_scope(connection, scope, locking=True)
    if command_id is not None:
        command = auth.one(connection, """SELECT codigo FROM comandos
            WHERE id_vehiculo=%s AND id_comando=%s AND activo=1""", (scope.vehicle_id, command_id))
        if command is None:
            raise ApplicationError(404, "Comando no disponible.", "platform_command")
        code = command["codigo"]
    if code is not None:
        _capability(connection, actor, unit, code)


def _verify_command(scope, code=None, *, command_id=None):
    with auth.manager.transaction() as connection:
        _verify_locked(connection, scope, code, command_id)


def validate_scope(scope):
    _verify_command(scope)


def request_scope(request):
    return getattr(request.state, "vehicle_scope", None) if request is not None else None


def command_policy(scope, command_id=None):
    def check(code=None, *, connection=None):
        if connection is not None:
            # Sigue siendo una sola transacción cuando el servicio ya abrió MySQL.
            return _verify_locked(connection, scope, code, command_id)
        try:
            _verify_command(scope, code, command_id=command_id)
        except ApplicationError as error:
            if error.status_code == 403:
                auth.note_denial(auth.Actor(scope.actor_id, scope.session_id, "", "", None),
                                 "comando", command_id if command_id is not None else code)
            raise
    return check


def check_vector(request, vector):
    scope = request_scope(request)
    if scope is not None:
        code = DIRECTION_CODES.get((vector.longitudinal, vector.lateral))
        policy = command_policy(scope)
        policy(code)
        return policy


def visible_since_assignment(scope, value):
    if scope is None or scope.history_since is None:
        return True
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return False
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value >= scope.history_since


def permitted_commands(request, commands):
    scope = request_scope(request)
    if scope is None:
        return commands
    with auth.manager.transaction(readonly=True) as connection:
        actor, unit = checked_scope(connection, scope)
        with auth.manager.cursor(connection, dictionary=True) as cursor:
            cursor.execute("SELECT codigo, invitado FROM iot43_capacidades WHERE id_modelo=%s AND control=1",
                           (unit["id_modelo"],))
            allowed = {row["codigo"] for row in cursor.fetchall() if actor.rol != "invitado" or row["invitado"]}
    return [command for command in commands if command.codigo in allowed and command.activo]


def _inspect_request(request, vehicle_id, action):
    actor = auth.authenticate_request(request)
    try:
        scope = _with_unit(actor, vehicle_id)
        demo = _DEMO_RUN.fullmatch(action)
        if request.method == "GET" and (action in _READ_ACTIONS or _DEMO_READ.fullmatch(action)):
            pass
        elif request.method == "POST" and (action in (_CONTROL_ACTIONS | {"movimientos", "detener"}) or demo):
            if scope.guest and (demo or action in _CONTROL_ACTIONS):
                raise _forbidden("La reserva solo autoriza pulsos de exhibición.")
            if scope.fisico and action not in {"detener", "control/liberar"} and not (demo and demo.group(2) == "cancelar"):
                capability = runtime.continuous.capability(vehicle_id)
                if not capability.habilitado or not capability.disponible:
                    raise ApplicationError(503, "El canal protegido del equipo no está disponible.", "platform_device")
        else:
            raise _forbidden("Gestiona la unidad mediante la plataforma 4.3.")
        return actor, scope
    except ApplicationError as error:
        if error.status_code in {403, 404}:
            auth.note_denial(actor, "vehiculo", vehicle_id)
        raise


async def gate_legacy(request, call_next):
    path = request.url.path.rstrip("/")
    vehicle = path == "/vehiculos" or path.startswith("/vehiculos/")
    evaluation = path == "/evaluacion" or path.startswith("/evaluacion/")
    if request.method == "OPTIONS" or not (vehicle or evaluation):
        return await call_next(request)
    try:
        if not auth.platform_enabled():
            return await call_next(request)
        if evaluation:
            raise ApplicationError(404, "Usa el acceso de la plataforma 4.3.", "platform_direct_disabled")
        match = _VEHICLE_PATH.fullmatch(path)
        if match is None or int(match.group(1)) > 2147483647:
            await run_in_threadpool(auth.authenticate_request, request)
            raise _forbidden("Usa el inventario autorizado de la plataforma 4.3.")
        vehicle_id = int(match.group(1))
        action = match.group(2) or ""
        actor, scope = await run_in_threadpool(_inspect_request, request, vehicle_id, action)
        request.state.actor = actor
        request.state.vehicle_scope = scope
        if scope.guest and request.method == "GET":
            if action in {"estado", "historial", "demos"}:
                return JSONResponse(None if action == "estado" else [], headers={"Cache-Control":"no-store"})
            if not action:
                return JSONResponse({"id_vehiculo": vehicle_id, "nombre": "Unidad de exhibición", "activo": True},
                                    headers={"Cache-Control":"no-store"})
            if _DEMO_READ.fullmatch(action):
                raise _forbidden("Las secuencias privadas requieren titularidad.")
        response = await call_next(request)
        if request.method == "GET" and response.status_code < 300:
            await run_in_threadpool(validate_scope, scope)
        response.headers["Cache-Control"] = "no-store"
        return response
    except ApplicationError as error:
        return JSONResponse({"detail": error.message}, status_code=error.status_code)
    except DatabaseUnavailableError:
        return JSONResponse({"detail": "Base de datos no disponible."}, status_code=503)


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id_vehiculo: int = Field(strict=True, ge=1, le=2147483647)


class TicketResponse(BaseModel):
    ticket: str
    expira_en: datetime


@router.post("/tickets", status_code=201, response_model=TicketResponse)
def ticket(payload: TicketRequest, actor: auth.ActorDep, response: Response):
    scope = _with_unit(actor, payload.id_vehiculo)
    raw = secrets.token_urlsafe(32)
    expires = auth.now_utc() + timedelta(seconds=20)
    with auth.manager.transaction() as connection:
        auth.one(connection, "SELECT id FROM iot43_sesiones WHERE id=%s FOR UPDATE", (scope.session_id,))
        checked_scope(connection, scope)
        auth.execute(connection, """DELETE FROM iot43_tickets
                WHERE (expires_at<=UTC_TIMESTAMP(6) OR used_at IS NOT NULL) AND id_sesion=%s""",
                (scope.session_id,))
        pending = auth.one(connection, "SELECT COUNT(*) AS total FROM iot43_tickets WHERE id_sesion=%s",
                           (scope.session_id,))
        if pending["total"] >= 8:
            raise ApplicationError(429, "Hay demasiados tickets pendientes.", "platform_ticket_rate")
        auth.execute(connection, """INSERT INTO iot43_tickets
            (token_hash, id_sesion, id_unidad, version_unidad, expires_at) VALUES(%s, %s, %s, %s, %s)""",
            (auth.digest(raw), scope.session_id, scope.unit_id, scope.version, expires))
    response.headers["Cache-Control"] = "no-store"
    return {"ticket": raw, "expira_en": auth.as_utc(expires)}


def open_monitor(websocket, vehicle_id):
    protocols = websocket.scope.get("subprotocols", ())
    if (websocket.headers.get("origin") not in _ORIGINS or len(protocols) != 2
            or protocols[0] != "node01" or not protocols[1].startswith("ticket.")):
        raise _forbidden("Se requiere un ticket de monitoreo.")
    raw = protocols[1][7:]
    if not auth.TOKEN.fullmatch(raw):
        raise _forbidden("Ticket inválido.")
    hashed = auth.digest(raw)
    with auth.manager.transaction() as connection:
        row = auth.one(connection, """SELECT id_sesion, id_unidad, version_unidad FROM iot43_tickets
            WHERE token_hash=%s AND used_at IS NULL AND expires_at>UTC_TIMESTAMP(6) FOR UPDATE""", (hashed,))
        if row is None:
            raise _forbidden("Ticket inválido, vencido o usado.")
        actor = auth.active_actor(connection, session_id=row["id_sesion"])
        unit = unit_scope(connection, actor, vehicle_id)
        if unit["id"] != row["id_unidad"] or unit["version"] != row["version_unidad"]:
            raise _forbidden("Ticket ajeno a esta asignación.")
        auth.execute(connection, "UPDATE iot43_tickets SET used_at=UTC_TIMESTAMP(6) WHERE token_hash=%s", (hashed,))
    return Scope(actor.id, actor.session_id, unit["id"], vehicle_id, unit["version"],
                 bool(unit["fisico"]) or vehicle_id == DEVICE_VEHICLE_ID, actor.rol == "invitado", unit["desde"])
