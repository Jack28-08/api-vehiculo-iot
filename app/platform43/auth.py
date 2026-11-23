"""Identidad local, sesiones opacas y auditoría sin credenciales."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import secrets
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from app.database.connection import MySQLConnectionManager
from app.errors import ApplicationError
from app.runtime import runtime


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IOT43_", env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True,
    )
    enabled: bool = False


@lru_cache
def settings():
    try:
        return PlatformSettings()
    except (ValidationError, SettingsError, OSError, UnicodeError):
        raise ApplicationError(503, "Configuración 4.3 no disponible.", "platform_config") from None


def platform_enabled():
    return settings().enabled


def require_enabled():
    if not platform_enabled():
        raise ApplicationError(404, "Plataforma 4.3 deshabilitada.", "platform_disabled")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usuario: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    contrasena: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ChangePassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    nueva: Annotated[str, StringConstraints(min_length=12, max_length=128)]


class Profile(BaseModel):
    id: int | None
    usuario: str | None
    rol: Literal["administrador", "comprador", "invitado"]
    nombre: str


class SessionResponse(BaseModel):
    token: str
    expira_en: datetime
    perfil: Profile


@dataclass(frozen=True)
class Actor:
    id: int | None
    session_id: int
    rol: str
    nombre: str
    usuario: str | None
    reservation_id: int | None = None

    def profile(self):
        return {"id": self.id, "usuario": self.usuario, "rol": self.rol, "nombre": self.nombre}


manager = MySQLConnectionManager()
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Sesion43")
router = APIRouter(prefix="/plataforma", tags=["Plataforma 4.3 • Identidad"])
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
SESSION_HOURS = 8


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value):
    return value.replace(tzinfo=timezone.utc)


def hash_password(password):
    salt = secrets.token_bytes(16)
    derived = _derive(password, salt)
    return f"scrypt-v1${salt.hex()}${derived.hex()}"


def _derive(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1,
                          maxmem=64 * 1024 * 1024, dklen=32)


def password_matches(encoded, password):
    try:
        version, salt, expected = encoded.split("$")
        salt, expected = bytes.fromhex(salt), bytes.fromhex(expected)
        if version != "scrypt-v1" or len(salt) != 16 or len(expected) != 32:
            return False
        return secrets.compare_digest(expected, _derive(password, salt))
    except (ValueError, AttributeError):
        return False


def digest(token):
    return hashlib.sha256(token.encode("ascii")).digest()


def execute(connection, statement, args=()):
    with manager.cursor(connection, dictionary=True) as cursor:
        cursor.execute(statement, args)
        return cursor.lastrowid


def one(connection, statement, args=()):
    with manager.cursor(connection, dictionary=True) as cursor:
        cursor.execute(statement, args)
        return cursor.fetchone()


def audit(connection, id_actor, accion, recurso, id_recurso, resultado="ok"):
    execute(connection, """INSERT INTO iot43_auditoria
            (id_actor, accion, recurso, id_recurso, resultado) VALUES (%s, %s, %s, %s, %s)""",
            (id_actor, accion, recurso, id_recurso, resultado))


def issue_session(connection, *, id_cuenta=None, id_reserva=None, expires_at=None):
    if (id_cuenta is None) == (id_reserva is None):
        raise ValueError("La sesión debe tener una sola identidad.")
    expires = expires_at if expires_at is not None else now_utc() + timedelta(hours=SESSION_HOURS)
    token = secrets.token_urlsafe(32)
    execute(connection, """INSERT INTO iot43_sesiones (token_hash, id_cuenta, id_reserva, expires_at)
            VALUES (%s, %s, %s, %s)""", (digest(token), id_cuenta, id_reserva, expires))
    return token, as_utc(expires)


_SESSIONS = """SELECT s.id AS session_id, s.id_cuenta, s.id_reserva, c.rol, c.nombre, c.correo
        FROM iot43_sesiones AS s
        LEFT JOIN iot43_cuentas AS c ON c.id = s.id_cuenta
        LEFT JOIN iot43_reservas AS r ON r.id = s.id_reserva
        WHERE s.revoked_at IS NULL AND s.expires_at > UTC_TIMESTAMP(6)
          AND ((s.id_cuenta IS NOT NULL AND c.activo = 1) OR
               (s.id_reserva IS NOT NULL AND r.ended_at IS NULL AND r.expires_at > UTC_TIMESTAMP(6)))"""


def active_actor(connection, *, token_hash=None, session_id=None, locking=False):
    ending = " FOR SHARE" if locking else ""
    if token_hash is not None:
        row = one(connection, _SESSIONS + " AND s.token_hash = %s" + ending, (token_hash,))
    else:
        row = one(connection, _SESSIONS + " AND s.id = %s" + ending, (session_id,))
    if row is None:
        raise ApplicationError(401, "Sesión ausente, inválida o vencida.", "platform_session")
    return Actor(row["id_cuenta"], row["session_id"], row["rol"] or "invitado",
                 row["nombre"] or "Invitado", row["correo"], row["id_reserva"])


def authenticate_request(request):
    require_enabled()
    authorization = request.headers.getlist("authorization")
    scheme, _, token = authorization[0].partition(" ") if len(authorization) == 1 else ("", "", "")
    if scheme.lower() != "bearer" or not TOKEN.fullmatch(token):
        raise ApplicationError(401, "Sesión requerida.", "platform_session")
    with manager.transaction(readonly=True) as connection:
        return active_actor(connection, token_hash=digest(token))


def require_actor(request: Request,
                  credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)] = None):
    if hasattr(request.state, "actor"):
        return request.state.actor
    actor = authenticate_request(request)
    request.state.actor = actor
    return actor


ActorDep = Annotated[Actor, Depends(require_actor)]


def require_admin(actor: ActorDep):
    if actor.rol != "administrador":
        note_denial(actor, "administracion", None)
        raise ApplicationError(403, "Se requiere un administrador.", "platform_forbidden")
    return actor


def note_denial(actor, resource, identifier):
    with manager.transaction() as connection:
        audit(connection, actor.id if actor else None, "acceso", resource, identifier, "rechazado")


def revoke_vehicle(id_vehiculo):
    if id_vehiculo is None:
        return
    runtime.continuous.stop(id_vehiculo)
    runtime.demos.signal_cancel(id_vehiculo)


def _revoke_actor_units(connection, actor):
    where = "id_cliente = %s" if actor.id is not None else "id = (SELECT id_unidad FROM iot43_reservas WHERE id = %s)"
    value = actor.id if actor.id is not None else actor.reservation_id
    with manager.cursor(connection, dictionary=True) as cursor:
        cursor.execute("SELECT id_vehiculo FROM iot43_unidades WHERE " + where, (value,))
        identifiers = cursor.fetchall()
    for row in identifiers:
        revoke_vehicle(row["id_vehiculo"])


class LoginQuota:
    def __init__(self):
        self._lock = Lock()
        self._attempts = {}

    def acquire(self, client, username):
        now = monotonic()
        with self._lock:
            self._attempts = {key: value for key, value in self._attempts.items() if value[0] > now}
            keys = (("peer", client), ("account", username))
            if len(self._attempts) >= 4096 or any(self._attempts.get(key, (0, 0))[1] >= limit
                                                for key, limit in zip(keys, (24, 8))):
                raise ApplicationError(429, "Espera antes de intentar el acceso otra vez.", "login_rate")
            for key in keys:
                end, count = self._attempts.get(key, (now + 300, 0))
                self._attempts[key] = (end, count + 1)


login_quota = LoginQuota()
_DUMMY_HASH = "scrypt-v1$" + "00" * 16 + "$" + "00" * 32


@router.get("/status")
def status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"habilitada": platform_enabled()}


@router.post("/auth/login", response_model=SessionResponse)
def login(payload: Login, request: Request, response: Response):
    require_enabled()
    username = payload.usuario.lower()
    login_quota.acquire(request.client.host if request.client else "", username)
    with manager.transaction() as connection:
        row = one(connection, "SELECT id, correo, nombre, rol, activo, hash_contrasena FROM iot43_cuentas WHERE correo=%s",
                  (username,))
        verified = password_matches(row["hash_contrasena"] if row else _DUMMY_HASH, payload.contrasena)
        account_id = row["id"] if row else None
        valid = row and row["activo"] and verified
        if valid:
            # Revalidar tras el hash permite revocar un login que coincidió con un bloqueo.
            current = one(connection, "SELECT activo, hash_contrasena FROM iot43_cuentas WHERE id=%s FOR UPDATE", (account_id,))
            valid = current and current["activo"] and current["hash_contrasena"] == row["hash_contrasena"]
        if valid:
            token, expires = issue_session(connection, id_cuenta=account_id)
        audit(connection, account_id, "login", "cuenta", account_id, "ok" if valid else "rechazado")
    if not valid:
        raise ApplicationError(401, "Credenciales inválidas.", "platform_login")
    response.headers["Cache-Control"] = "no-store"
    profile = Actor(account_id, 0, row["rol"], row["nombre"], row["correo"]).profile()
    return {"token": token, "expira_en": expires, "perfil": profile}


@router.get("/perfil", response_model=Profile)
def profile(actor: ActorDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return actor.profile()


@router.post("/auth/logout", status_code=204)
def logout(actor: ActorDep):
    with manager.transaction() as connection:
        execute(connection, "UPDATE iot43_sesiones SET revoked_at=UTC_TIMESTAMP(6) WHERE id=%s", (actor.session_id,))
        if actor.reservation_id is not None:
            execute(connection, "UPDATE iot43_reservas SET ended_at=UTC_TIMESTAMP(6) WHERE id=%s AND ended_at IS NULL",
                    (actor.reservation_id,))
        _revoke_actor_units(connection, actor)
        audit(connection, actor.id, "logout", "sesion", actor.session_id)


@router.post("/auth/contrasena", status_code=204)
def change_password(payload: ChangePassword, actor: ActorDep):
    if actor.id is None:
        raise ApplicationError(403, "Esta sesión no tiene contraseña.", "platform_forbidden")
    login_quota.acquire("password-change", str(actor.id))
    with manager.transaction() as connection:
        current = one(connection, "SELECT hash_contrasena FROM iot43_cuentas WHERE id=%s AND activo=1 FOR UPDATE",
                      (actor.id,))
        allowed = current and password_matches(current["hash_contrasena"], payload.actual)
        if allowed:
            execute(connection, "UPDATE iot43_cuentas SET hash_contrasena=%s WHERE id=%s",
                    (hash_password(payload.nueva), actor.id))
            execute(connection, "UPDATE iot43_sesiones SET revoked_at=UTC_TIMESTAMP(6) WHERE id_cuenta=%s AND revoked_at IS NULL",
                    (actor.id,))
            _revoke_actor_units(connection, actor)
        audit(connection, actor.id, "cambio_contrasena", "cuenta", actor.id,
              "ok" if allowed else "rechazado")
    if not allowed:
        raise ApplicationError(401, "Credenciales inválidas.", "platform_login")
