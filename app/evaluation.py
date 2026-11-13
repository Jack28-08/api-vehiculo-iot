"""Acceso temporal y limitado al control del vehículo 1."""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from threading import Lock
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response

PAGES_ORIGIN = "https://jack28-08.github.io"
WINDOW_FILE = Path("C:/IoT/servicios/api/evaluacion-4-2.json")
SESSION_MAX = 256
SESSION_TTL = timedelta(minutes=10)
MAX_WINDOW = timedelta(hours=2)
NO_STORE = {"Cache-Control": "no-store"}
OPERATIONS = {
    "issue": (8, 48),
    "move": (45, 120),
    "start": (4, 12),
    "stop": (30, 90),
    "cancel": (24, 72),
}
CONTROL_PATHS = (
    (re.compile(r"/vehiculos/1/movimientos"), "move"),
    (re.compile(r"/vehiculos/1/demos/[1-9][0-9]{0,9}/iniciar"), "start"),
    (re.compile(r"/vehiculos/1/demos/[1-9][0-9]{0,9}/cancelar"), "cancel"),
    (re.compile(r"/vehiculos/1/detener"), "stop"),
)


@dataclass(frozen=True, slots=True)
class Window:
    identifier: UUID
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class Session:
    client: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EvaluationPrincipal:
    """Capacidad de una operación concreta, nunca un rol de operador general."""

    operation: str


def utc_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("UTC explícito requerido")
    return parsed


def denied(status: int, detail: str, **headers: str) -> HTTPException:
    return HTTPException(status, detail, headers={**NO_STORE, **headers})


class EvaluationAccess:
    """Sesiones opacas en memoria; la configuración local se revalida por solicitud."""

    def __init__(self, path=WINDOW_FILE, *, now=None, ticks=None):
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ticks = ticks or time.monotonic
        self._lock = Lock()
        self._window = None
        self._sessions: dict[bytes, Session] = {}
        self._traffic = defaultdict(deque)

    def _active_window(self, now: datetime) -> Window:
        window = None
        if self.path.is_absolute():
            try:
                with self.path.open("rb") as source:
                    raw = source.read(2049)
                if len(raw) > 2048:
                    raise ValueError("configuración demasiado grande")
                data = json.loads(raw)
                if not isinstance(data, dict) or set(data) != {"id", "starts_at", "ends_at"}:
                    raise ValueError("configuración no reconocida")
                candidate = Window(UUID(data["id"]), utc_date(data["starts_at"]), utc_date(data["ends_at"]))
                if candidate.starts_at <= now < candidate.ends_at and candidate.ends_at - candidate.starts_at <= MAX_WINDOW:
                    window = candidate
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if window != self._window:
            self._sessions.clear()
            self._traffic.clear()
            self._window = window
        if window is None:
            raise denied(403, "La evaluación no está disponible.")
        return window

    @staticmethod
    def _client(request: Request) -> str:
        if request.headers.get("origin") != PAGES_ORIGIN or not request.client:
            raise denied(403, "La evaluación no está disponible para este cliente.")
        # La dirección procede de Uvicorn y su proxy de loopback de confianza.
        return request.client.host

    def _consume(self, operation: str, client: str) -> None:
        tick = self._ticks()
        for key, queue in tuple(self._traffic.items()):
            while queue and queue[0] <= tick - 60:
                queue.popleft()
            if not queue:
                del self._traffic[key]
        global_key = (operation, None)
        client_key = (operation, client)
        client_max, global_max = OPERATIONS[operation]
        if len(self._traffic.get(global_key, ())) >= global_max or len(self._traffic.get(client_key, ())) >= client_max:
            raise denied(429, "Espera un momento antes de volver a intentarlo.", **{"Retry-After": "60"})
        self._traffic[global_key].append(tick)
        self._traffic[client_key].append(tick)

    def issue(self, request: Request) -> tuple[str, datetime]:
        client = self._client(request)
        with self._lock:
            now = self._now()
            window = self._active_window(now)
            for key, session in tuple(self._sessions.items()):
                if session.expires_at <= now:
                    del self._sessions[key]
            if len(self._sessions) >= SESSION_MAX:
                raise denied(429, "No hay cupo de evaluación en este momento.", **{"Retry-After": "60"})
            self._consume("issue", client)
            token = secrets.token_urlsafe(32)
            expires = min(now + SESSION_TTL, window.ends_at)
            self._sessions[hashlib.sha256(token.encode("ascii")).digest()] = Session(client, expires)
            return token, expires

    def authorize(self, request: Request) -> EvaluationPrincipal:
        operation = next((name for path, name in CONTROL_PATHS if path.fullmatch(request.scope["path"])), None)
        if request.method != "POST" or operation is None:
            raise denied(403, "Acción fuera del alcance de la evaluación.")
        client = self._client(request)
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", request.headers.get("authorization", ""), re.IGNORECASE | re.ASCII)
        with self._lock:
            now = self._now()
            self._active_window(now)
            token = match.group(1) if match else ""
            session = self._sessions.get(hashlib.sha256(token.encode("ascii")).digest())
            if session is None or session.expires_at <= now or session.client != client:
                raise denied(401, "La sesión de evaluación no es válida.", **{"WWW-Authenticate": "Bearer"})
            self._consume(operation, client)
            return EvaluationPrincipal(operation)


evaluation_access = EvaluationAccess()
router = APIRouter(tags=["Evaluación temporal"])


@router.get("/evaluacion/vehiculos/1")
def open_evaluation(request: Request, response: Response):
    """Emite una sesión sin clave únicamente mientras la ventana está activa."""
    token, expires = evaluation_access.issue(request)
    response.headers.update(NO_STORE)
    return {"token": token, "expires_at": expires.isoformat()}
