"""Coordinación local por vehículo, compartida entre workers HTTP y tareas DEMO."""

from dataclasses import dataclass, field
import threading

from app.errors import ApplicationError


@dataclass
class VehicleSlot:
    lock: threading.RLock = field(default_factory=threading.RLock)
    active: object | None = None
    stops: int = 0


class VehicleCoordinator:
    def __init__(self):
        self._guard = threading.RLock()
        self._slots: dict[int, VehicleSlot] = {}

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
