"""Coordinación local por vehículo, compartida entre workers HTTP y tareas DEMO."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time

from app.errors import ApplicationError


@dataclass
class VehicleSlot:
    lock: threading.RLock = field(default_factory=threading.RLock)
    active: object | None = None
    stops: int = 0
    legacy_writes: int = 0
    continuous_deadline: float = 0


class VehicleCoordinator:
    def __init__(self, clock=time.monotonic):
        self._guard = threading.RLock()
        self._slots: dict[int, VehicleSlot] = {}
        self._clock = clock

    def _slot(self, identifier):
        with self._guard:
            return self._slots.setdefault(identifier, VehicleSlot())

    def lock_for(self, identifier):
        return self._slot(identifier).lock

    def get_active(self, identifier):
        with self._guard:
            return self._slot(identifier).active

    def active_executions(self):
        with self._guard:
            return [s.active for s in self._slots.values() if s.active is not None]

    def ensure_available(self, identifier):
        with self._guard:
            slot = self._slot(identifier)
            if slot.active is not None or slot.stops:
                raise ApplicationError(409, "El vehículo tiene una DEMO o detención en curso.", "vehicle_busy")
            if slot.continuous_deadline > self._clock():
                raise ApplicationError(409, "El vehículo tiene un permiso de control continuo.", "vehicle_busy")

    @contextmanager
    def legacy_mutation(self, identifier):
        # El mutex lento de la transacción se adquiere fuera de este método.
        with self._guard:
            slot = self._slot(identifier)
            self.ensure_available(identifier)
            slot.legacy_writes += 1
        try:
            yield
        finally:
            with self._guard:
                slot.legacy_writes -= 1

    def reserve_continuous(self, identifier, deadline):
        with self._guard:
            slot = self._slot(identifier)
            self.ensure_available(identifier)
            if slot.legacy_writes:
                raise ApplicationError(409, "El vehículo tiene una escritura en curso.", "vehicle_busy")
            slot.continuous_deadline = deadline

    def continuous_active(self, identifier):
        with self._guard:
            slot = self._slot(identifier)
            return not slot.stops and slot.continuous_deadline > self._clock()

    def renew_continuous(self, identifier, deadline):
        with self._guard:
            if not self.continuous_active(identifier):
                raise ApplicationError(409, "El permiso continuo ya no está vigente.", "lease_missing")
            self._slot(identifier).continuous_deadline = deadline

    def clear_continuous(self, identifier):
        with self._guard:
            self._slot(identifier).continuous_deadline = 0

    def reserve(self, identifier, execution):
        with self._guard:
            self.ensure_available(identifier)
            self._slot(identifier).active = execution

    def clear_if_owner(self, identifier, execution_id):
        with self._guard:
            slot = self._slot(identifier)
            if slot.active is not None and slot.active.id_ejecucion == execution_id:
                slot.active = None
                return True
            return False

    def begin_stop(self, identifier):
        with self._guard:
            self._slot(identifier).stops += 1

    def end_stop(self, identifier):
        with self._guard:
            self._slot(identifier).stops -= 1
