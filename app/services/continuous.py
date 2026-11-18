"""Permisos de corta duración para el canal autenticado del dispositivo."""

from dataclasses import dataclass
import math
from pathlib import Path
import secrets
from threading import RLock
import time
from uuid import UUID, uuid4

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from app.errors import ApplicationError
from app.models.continuous import Capability, ControlGrant, DeviceSnapshot, ReleaseResult


VEHICLE_ID = 1
TTL_MS = 750
RENEW_MS = 200
DEVICE_TIMEOUT = TTL_MS / 1000


class DeviceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IOT_CONTINUOUS_",
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    enabled: bool = False
    diagonals: bool = False
    device_token: SecretStr = Field(default=SecretStr(""), repr=False)

    @classmethod
    def from_environment(cls):
        try:
            return cls()
        except (ValidationError, SettingsError, OSError, UnicodeError):
            # Un fallo de configuración deja el canal indisponible y evita el fallback.
            return cls.model_construct(enabled=True, diagonals=False, device_token=SecretStr(""))

    def authenticates(self, header):
        token = self.device_token.get_secret_value()
        if not 32 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
            return False
        if not header or not header.isascii() or len(header) != len(token) + len("Bearer "):
            return False
        return secrets.compare_digest(header.encode(), ("Bearer " + token).encode())


@dataclass(eq=False)
class DeviceConnection:
    last_heartbeat: float | None = None
    last_challenge: int = 0
    sequence: int = 0


@dataclass
class Lease:
    gesture: UUID
    sequence: int
    deadline: float
    longitudinal: int = 0
    lateral: int = 0


class ContinuousControl:
    """Coordina un vehículo por proceso. No encola acciones ni escribe en RDS."""

    def __init__(self, coordinator, settings=None, clock=time.monotonic):
        self._coordinator = coordinator
        self._settings = settings
        self._clock = clock
        self._guard = RLock()
        self._barrier = uuid4()
        self._lease: Lease | None = None
        self._device: DeviceConnection | None = None

    @property
    def _policy(self):
        with self._guard:
            if self._settings is None:
                self._settings = DeviceSettings.from_environment()
            return self._settings

    def _enabled(self, vehicle_id):
        return vehicle_id == VEHICLE_ID and self._policy.enabled

    def authenticates(self, vehicle_id, authorization):
        return self._enabled(vehicle_id) and self._policy.authenticates(authorization)

    def _require_enabled(self, vehicle_id):
        if not self._enabled(vehicle_id):
            raise ApplicationError(404, "Control continuo no habilitado para este vehículo.", "control_disabled")

    def _ready(self, now):
        last = self._device.last_heartbeat if self._device else None
        return last is not None and now - last < DEVICE_TIMEOUT

    def _invalidate(self):
        self._lease = None
        self._barrier = uuid4()
        self._coordinator.clear_continuous(VEHICLE_ID)

    def _expire(self, now):
        if self._lease and (now >= self._lease.deadline or not self._ready(now)
                            or not self._coordinator.continuous_active(VEHICLE_ID)):
            self._invalidate()

    def capability(self, vehicle_id):
        enabled = self._enabled(vehicle_id)
        with self._guard:
            now = self._clock()
            if enabled:
                self._expire(now)
            return Capability(habilitado=enabled, disponible=enabled and self._ready(now),
                              diagonales=enabled and self._policy.diagonals,
                              barrera=self._barrier if enabled else None,
                              ttl_ms=TTL_MS, renovar_ms=RENEW_MS)

    def _validate_barrier(self, value):
        if value != self._barrier:
            raise ApplicationError(409, "El gesto ya no pertenece a la barrera vigente.", "stale_barrier")

    def _validate_vector(self, vector):
        if not (vector.longitudinal or vector.lateral):
            raise ApplicationError(422, "Usa liberar para volver a neutral.", "neutral_vector")
        if vector.longitudinal and vector.lateral and not self._policy.diagonals:
            raise ApplicationError(409, "El control diagonal no está habilitado.", "diagonals_disabled")

    def _grant(self):
        return ControlGrant(barrera=self._barrier, gesto=self._lease.gesture,
                            secuencia=self._lease.sequence, ttl_ms=TTL_MS)

    def acquire(self, vehicle_id, vector):
        self._require_enabled(vehicle_id)
        with self._guard:
            now = self._clock()
            self._expire(now)
            self._validate_barrier(vector.barrera)
            if not self._ready(now):
                raise ApplicationError(503, "No hay sesión reciente del dispositivo.", "device_unavailable")
            if vector.secuencia != 1:
                raise ApplicationError(409, "El permiso requiere la secuencia inicial.", "invalid_sequence")
            self._validate_vector(vector)
            if self._lease is not None:
                raise ApplicationError(409, "El vehículo ya tiene un permiso vigente.", "lease_busy")
            deadline = now + TTL_MS / 1000
            self._coordinator.reserve_continuous(vehicle_id, deadline)
            # La concesión no mueve el dispositivo, incluso si llega después de que el dedo se levanta.
            self._lease = Lease(vector.gesto, vector.secuencia, deadline)
            return self._grant()

    def update(self, vehicle_id, vector):
        self._require_enabled(vehicle_id)
        with self._guard:
            now = self._clock()
            self._expire(now)
            self._validate_barrier(vector.barrera)
            lease = self._lease
            if lease is None or lease.gesture != vector.gesto:
                raise ApplicationError(409, "El gesto no tiene un permiso vigente.", "lease_missing")
            if vector.secuencia <= lease.sequence:
                raise ApplicationError(409, "El vector llegó repetido o fuera de orden.", "stale_sequence")
            self._validate_vector(vector)
            deadline = now + TTL_MS / 1000
            self._coordinator.renew_continuous(vehicle_id, deadline)
            lease.sequence = vector.secuencia
            lease.deadline = deadline
            lease.longitudinal, lease.lateral = vector.longitudinal, vector.lateral
            return self._grant()

    def release(self, vehicle_id, gesture):
        self._require_enabled(vehicle_id)
        with self._guard:
            self._expire(self._clock())
            if gesture.barrera == self._barrier:
                if self._lease is not None and self._lease.gesture != gesture.gesto:
                    raise ApplicationError(409, "El permiso pertenece a otro gesto.", "different_gesture")
                # También invalida un permiso que todavía no llegó al servidor.
                self._invalidate()
            return ReleaseResult(barrera=self._barrier)

    def stop(self, vehicle_id):
        if self._enabled(vehicle_id):
            with self._guard:
                self._invalidate()

    def connect(self):
        with self._guard:
            self._invalidate()
            self._device = DeviceConnection()
            return self._device

    def heartbeat(self, device, challenge):
        with self._guard:
            if device is not self._device or challenge <= device.last_challenge:
                raise ValueError("Sesión o reto del dispositivo obsoleto.")
            now = self._clock()
            self._expire(now)
            if device.last_heartbeat is not None and not self._ready(now):
                self._invalidate()
            device.last_heartbeat = now
            device.last_challenge = challenge
            device.sequence += 1
            lease = self._lease
            moving = lease is not None and (lease.longitudinal or lease.lateral)
            ttl = math.floor((lease.deadline - now) * 1000) if moving else 0
            return DeviceSnapshot(reto=challenge, secuencia=device.sequence,
                                  vigencia_ms=min(TTL_MS, ttl) if ttl > 0 else 0,
                                  longitudinal=lease.longitudinal if ttl > 0 else 0,
                                  lateral=lease.lateral if ttl > 0 else 0)

    def disconnect(self, device):
        with self._guard:
            if device is self._device:
                self._device = None
                self._invalidate()

    def close(self):
        with self._guard:
            if self._device is not None or self._lease is not None:
                self._device = None
                self._invalidate()
