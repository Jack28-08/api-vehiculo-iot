"""Permisos de corta duración para el canal autenticado del dispositivo."""

from dataclasses import dataclass, field
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
LEGACY_DURATIONS_MS = {1: 700, 2: 900, 3: 250, 4: 1000, 5: 900, 6: 350, 7: 1000, 8: 700}
REMOTE_DEMO_CODE = 6
DIAGONAL_NAMES = {9: "AVANZAR_IZQUIERDA", 10: "AVANZAR_DERECHA",
                  11: "RETROCEDER_IZQUIERDA", 12: "RETROCEDER_DERECHA"}
DIAGONAL_MASKS = {9: 0x03, 10: 0x82, 11: 0x11, 12: 0x90}
DIRECTION_CODES = {(0, -1): 1, (1, 0): 2, (-1, 0): 5, (0, 1): 8,
                   (1, -1): 9, (1, 1): 10, (-1, -1): 11, (-1, 1): 12}
DIRECTION_NAMES = {1: {"GI", "GIRAR_IZQUIERDA"}, 2: {"AVANZAR"},
                   5: {"RETROCEDER"}, 8: {"GIRAR_DERECHA"},
                   **{code: {name} for code, name in DIAGONAL_NAMES.items()}}
ACK_WINDOW = 16
PULSE_REQUEST_LIMIT = 512


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
    remote_demo: bool = False
    persist_transitions: bool = False
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
    pulse: int = 0
    offered: dict = field(default_factory=dict)
    last_ack: int = 0
    last_output: tuple | None = None


@dataclass
class Lease:
    gesture: UUID
    sequence: int
    deadline: float
    longitudinal: int = 0
    lateral: int = 0
    operation: UUID | None = None
    history_id: int | None = None
    recording: bool = False


@dataclass(frozen=True)
class Pulse:
    code: int
    identifier: int
    deadline: float
    operation: UUID


class ContinuousControl:
    """Coordina un vehículo por proceso. Las escrituras se ejecutan sin el mutex del canal."""

    def __init__(self, coordinator, settings=None, clock=time.monotonic, *, record_transition=None):
        self._coordinator = coordinator
        self._settings = settings
        self._clock = clock
        self._guard = RLock()
        self._barrier = uuid4()
        self._neutral_operation = uuid4()
        self._record_transition = record_transition
        self._pulse_requests = set()
        self._lease: Lease | None = None
        self._pulse: Pulse | None = None
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
        self._pulse = None
        self._barrier = uuid4()
        self._neutral_operation = uuid4()
        self._pulse_requests.clear()
        self._coordinator.clear_continuous(VEHICLE_ID)

    def _expire(self, now):
        if self._lease and (now >= self._lease.deadline or not self._ready(now)
                            or not self._coordinator.continuous_active(VEHICLE_ID)):
            self._invalidate()
        if self._pulse and (not self._ready(now) or self._coordinator.stopping(VEHICLE_ID)):
            self._invalidate()
        if self._pulse and now >= self._pulse.deadline:
            self._pulse = None
            self._neutral_operation = uuid4()

    def capability(self, vehicle_id):
        enabled = self._enabled(vehicle_id)
        with self._guard:
            now = self._clock()
            if enabled:
                self._expire(now)
            return Capability(habilitado=enabled, disponible=enabled and self._ready(now),
                              diagonales=enabled and self._policy.diagonals,
                              demo_remota=enabled and self._policy.remote_demo,
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

    def _check_pulse_idle(self):
        if self._pulse is not None:
            raise ApplicationError(409, "El vehículo tiene un pulso vigente.", "pulse_busy")

    def pulse_ticket(self, vehicle_id, barrier):
        """Comprueba el encabezado antes de adquirir el mutex que puede esperar a RDS."""
        if not self._enabled(vehicle_id):
            return None
        with self._guard:
            self._expire(self._clock())
            self.validate_pulse_ticket(vehicle_id, barrier)
            self._check_pulse_idle()
            return str(self._barrier)

    def validate_pulse_ticket(self, vehicle_id, ticket):
        with self._guard:
            if ticket != str(self._barrier) or self._coordinator.stopping(vehicle_id):
                raise ApplicationError(409, "La barrera del dispositivo ya no está vigente.", "stale_barrier")
            if not self._ready(self._clock()):
                raise ApplicationError(503, "No hay sesión reciente del dispositivo.", "device_unavailable")

    def claim_pulse_request(self, vehicle_id, ticket, request_id):
        if request_id is None:
            return
        with self._guard:
            self.validate_pulse_ticket(vehicle_id, ticket)
            if request_id in self._pulse_requests:
                raise ApplicationError(409, "La solicitud de movimiento ya se recibió.", "duplicate_request")
            if len(self._pulse_requests) >= PULSE_REQUEST_LIMIT:
                self._invalidate()
                raise ApplicationError(409, "La barrera del dispositivo se renovó.", "stale_barrier")
            self._pulse_requests.add(request_id)

    def validate_pulse_code(self, code, name=None):
        if type(code) is int and code in DIAGONAL_NAMES:
            if self._policy.diagonals and (name is None or name == DIAGONAL_NAMES[code]):
                return
            raise ApplicationError(409, "La diagonal requiere el catálogo y la habilitación del equipo.", "diagonals_disabled")
        if type(code) is not int or code not in LEGACY_DURATIONS_MS:
            raise ApplicationError(409, "El código no tiene actuación física configurada.", "invalid_pulse_code")
        if code == REMOTE_DEMO_CODE and not self._policy.remote_demo:
            raise ApplicationError(409, "La DEMO autónoma del equipo no está habilitada.", "remote_demo_disabled")

    def dispatch_pulse(self, vehicle_id, ticket, code, duration_ms=None, *, name=None, operation=None):
        with self._guard:
            now = self._clock()
            self._expire(now)
            try:
                self.validate_pulse_ticket(vehicle_id, ticket)
                self.validate_pulse_code(code, name)
                self._check_pulse_idle()
                if self._lease is not None:
                    return False
            except ApplicationError:
                # El commit lógico anterior sigue confirmado, incluso si el equipo ya no admite su pulso.
                return False
            device = self._device
            device.pulse += 1
            duration = LEGACY_DURATIONS_MS.get(code, 900) if duration_ms is None else duration_ms
            self._pulse = Pulse(code, device.pulse, now + duration / 1000, operation or uuid4())
            return True

    def cancel_demo_pulses(self, vehicle_id, ticket):
        if not self._enabled(vehicle_id):
            return
        with self._guard:
            self._expire(self._clock())
            if ticket == str(self._barrier) or (ticket is None and self._lease is None and self._pulse is None):
                self._invalidate()

    def end_demo_step(self, ticket):
        with self._guard:
            if ticket == str(self._barrier) and self._pulse is not None:
                self._pulse = None
                self._neutral_operation = uuid4()

    def _grant(self):
        return ControlGrant(barrera=self._barrier, gesto=self._lease.gesture,
                            secuencia=self._lease.sequence, ttl_ms=TTL_MS,
                            id_operacion=self._lease.operation, id_historial=self._lease.history_id)

    def _update_lease(self, vector):
        self._expire(self._clock())
        self._validate_barrier(vector.barrera)
        lease = self._lease
        if lease is None or lease.gesture != vector.gesto:
            raise ApplicationError(409, "El gesto no tiene un permiso vigente.", "lease_missing")
        if vector.secuencia <= lease.sequence:
            raise ApplicationError(409, "El vector llegó repetido o fuera de orden.", "stale_sequence")
        return lease

    def _apply(self, vehicle_id, lease, vector, operation, history_id):
        deadline = self._clock() + TTL_MS / 1000
        self._coordinator.renew_continuous(vehicle_id, deadline)
        lease.sequence = vector.secuencia
        lease.deadline = deadline
        lease.longitudinal, lease.lateral = vector.longitudinal, vector.lateral
        lease.operation, lease.history_id = operation, history_id
        lease.recording = False
        return self._grant()

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
            self._check_pulse_idle()
            if self._lease is not None:
                raise ApplicationError(409, "El vehículo ya tiene un permiso vigente.", "lease_busy")
            deadline = now + TTL_MS / 1000
            self._coordinator.reserve_continuous(vehicle_id, deadline)
            # La concesión no mueve el dispositivo, incluso si llega después de que el dedo se levanta.
            self._lease = Lease(vector.gesto, vector.secuencia, deadline)
            return self._grant()

    def update(self, vehicle_id, vector, *, platform_policy=None):
        self._require_enabled(vehicle_id)
        axes = (vector.longitudinal, vector.lateral)
        with self._guard:
            lease = self._update_lease(vector)
            self._validate_vector(vector)
            if lease.recording:
                raise ApplicationError(409, "Se está confirmando el cambio de dirección.", "transition_busy")
            if axes == (lease.longitudinal, lease.lateral):
                return self._apply(vehicle_id, lease, vector, lease.operation, lease.history_id)
            operation = uuid4()
            if not self._policy.persist_transitions:
                return self._apply(vehicle_id, lease, vector, operation, None)
            if self._record_transition is None:
                raise ApplicationError(503, "No se puede verificar el catálogo.", "transition_unavailable")
            lease.recording = True
            lease.longitudinal = lease.lateral = 0
            self._neutral_operation = uuid4()

        code = DIRECTION_CODES[axes]

        def validate(connection=None):
            if connection is not None and platform_policy is not None:
                platform_policy(code, connection=connection)
            with self._guard:
                self._update_lease(vector)

        try:
            started = self._clock()
            history_id, timings = self._record_transition(vehicle_id, code, DIRECTION_NAMES[code], operation, validate)
            elapsed = round(max(0, self._clock() - started) * 1000)
            with self._guard:
                self._update_lease(vector)
                grant = self._apply(vehicle_id, lease, vector, operation, history_id)
                return grant.model_copy(update={"ms_persistencia": elapsed, "tiempos_ms": timings})
        except Exception:
            with self._guard:
                if self._lease is lease:
                    self._invalidate()
            raise

    def release(self, vehicle_id, gesture):
        self._require_enabled(vehicle_id)
        with self._guard:
            self._expire(self._clock())
            if gesture.barrera == self._barrier:
                if self._lease is not None and self._lease.gesture != gesture.gesto:
                    raise ApplicationError(409, "El permiso pertenece a otro gesto.", "different_gesture")
                if self._pulse is not None or self._coordinator.get_active(vehicle_id) is not None:
                    raise ApplicationError(409, "El gesto no es dueño del pulso o DEMO.", "different_gesture")
                # También invalida un permiso que todavía no llegó al servidor.
                self._invalidate()
            return ReleaseResult(barrera=self._barrier, id_operacion=self._neutral_operation)

    def stop(self, vehicle_id):
        if self._enabled(vehicle_id):
            with self._guard:
                self._invalidate()
                return self._neutral_operation

    def _offer(self, device, snapshot, operation, mask, now):
        device.offered[snapshot.secuencia] = (operation, mask, now)
        if len(device.offered) > ACK_WINDOW:
            del device.offered[next(iter(device.offered))]
        return snapshot

    def report(self, device, ack):
        """Correlaciona solamente consignas de este enlace. Nunca cambia sus salidas."""
        with self._guard:
            if device is not self._device or not device.last_ack <= ack.secuencia < device.sequence:
                return None
            offered = device.offered.get(ack.secuencia)
            if offered is None:
                return None
            operation, mask, sent_at = offered
            if (ack.estado == "aplicado" and (not mask or ack.mascara != mask)
                    or ack.estado == "liberado" and ack.mascara != 0):
                return None
            state = (operation, ack.estado, ack.mascara, ack.simulado)
            if ack.secuencia == device.last_ack and ack.estado == "aplicado":
                return None
            device.last_ack = ack.secuencia
            if device.last_output == state:
                return None
            device.last_output = state
            return dict(id_operacion=operation, estado_salida=ack.estado, mascara=ack.mascara,
                        simulado=ack.simulado, ms_ida_vuelta=round(max(0, self._clock() - sent_at) * 1000))

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
            if self._pulse is not None:
                pulse = self._pulse
                ttl = math.floor((pulse.deadline - now) * 1000)
                if ttl > 0:
                    snapshot = DeviceSnapshot(reto=challenge, secuencia=device.sequence, vigencia_ms=min(TTL_MS, ttl),
                                              codigo=pulse.code, pulso=pulse.identifier)
                    mask = DIAGONAL_MASKS[pulse.code] if pulse.code in DIAGONAL_MASKS else 1 << (pulse.code - 1)
                    return self._offer(device, snapshot, pulse.operation, mask, now)
                self._pulse = None
                self._neutral_operation = uuid4()
            lease = self._lease
            moving = lease is not None and (lease.longitudinal or lease.lateral)
            ttl = math.floor((lease.deadline - now) * 1000) if moving else 0
            snapshot = DeviceSnapshot(reto=challenge, secuencia=device.sequence,
                                      vigencia_ms=min(TTL_MS, ttl) if ttl > 0 else 0,
                                      longitudinal=lease.longitudinal if ttl > 0 else 0,
                                      lateral=lease.lateral if ttl > 0 else 0)
            if ttl > 0:
                code = DIRECTION_CODES[(lease.longitudinal, lease.lateral)]
                mask = DIAGONAL_MASKS[code] if code in DIAGONAL_MASKS else 1 << (code - 1)
                return self._offer(device, snapshot, lease.operation, mask, now)
            return self._offer(device, snapshot, self._neutral_operation, 0, now)

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
