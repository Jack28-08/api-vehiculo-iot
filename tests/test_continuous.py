"""Pruebas locales del broker. Ni ASGI ni los dobles acceden a AWS."""

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.errors import ApplicationError
from app.main import app
from app.models.continuous import ControlVector, Gesture
from app.models.vehiculos import MovimientoConfirmado
from app.routes.vehiculos import get_service
from app.runtime import AppRuntime, CoordinatedVehiculoService
from app.security import ApiKeySettings, get_api_key_settings
from app.services.continuous import ContinuousControl, DeviceSettings, TTL_MS
from app.services.coordination import VehicleCoordinator
from app.services.demo_data import DemoPlan
from tests.test_api_security import main_request
from tests.test_cors import cors_request
from tests.test_http import MOVIMIENTO
from tests.test_realtime import until


CONTROL = "/vehiculos/1/control"
DEVICE = "/ws/dispositivos/1"
OPERATOR = "fixture-local-operator"
DEVICE_TOKEN = "fixture-local-device-" * 2


def device_settings(*, diagonals=False):
    return DeviceSettings(_env_file=None, enabled=True, diagonals=diagonals, device_token=DEVICE_TOKEN)


def assert_neutral(test, snapshot):
    test.assertEqual((snapshot.longitudinal, snapshot.lateral, snapshot.vigencia_ms), (0, 0, 0))


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = lambda: self.now
        self.coordinator = VehicleCoordinator(clock)
        self.broker = ContinuousControl(self.coordinator, device_settings(diagonals=True), clock)
        self.device = self.broker.connect()
        self.broker.heartbeat(self.device, 1)
        self.vector = ControlVector(barrera=self.broker.capability(1).barrera, gesto=uuid4(),
                                    secuencia=1, longitudinal=1, lateral=-1)

    def test_permit_is_neutral_then_vector_updates_until_expiry(self):
        grant = self.broker.acquire(1, self.vector)
        self.assertEqual(grant.ttl_ms, TTL_MS)
        assert_neutral(self, self.broker.heartbeat(self.device, 2))
        vector = self.vector.model_copy(update={"secuencia": 2})
        self.broker.update(1, vector)
        self.now += .15
        active = self.broker.heartbeat(self.device, 3)
        self.assertEqual((active.longitudinal, active.lateral, active.reto, active.secuencia), (1, -1, 3, 3))
        self.assertGreater(active.vigencia_ms, 0)
        self.assertLess(active.vigencia_ms, TTL_MS)
        self.now += .2
        self.broker.update(1, self.vector.model_copy(update={"secuencia": 3, "lateral": 1}))
        self.now += .2
        active = self.broker.heartbeat(self.device, 4)
        self.assertEqual(active.lateral, 1)
        self.now += .56
        assert_neutral(self, self.broker.heartbeat(self.device, 5))
        self.assertNotEqual(self.broker.capability(1).barrera, self.vector.barrera)
        with self.assertRaises(ApplicationError) as late:
            self.broker.update(1, self.vector.model_copy(update={"secuencia": 4}))
        self.assertEqual(late.exception.code, "stale_barrier")

    def test_release_before_acquire_and_stale_release_cannot_close_another_gesture(self):
        first = Gesture(barrera=self.vector.barrera, gesto=self.vector.gesto)
        released = self.broker.release(1, first)
        with self.assertRaises(ApplicationError) as late:
            self.broker.acquire(1, self.vector)
        self.assertEqual(late.exception.code, "stale_barrier")
        second = self.vector.model_copy(update={"barrera": released.barrera, "gesto": uuid4()})
        self.broker.acquire(1, second)
        self.broker.update(1, second.model_copy(update={"secuencia": 2}))
        self.assertEqual(self.broker.release(1, first).barrera, second.barrera)
        with self.assertRaises(ApplicationError) as foreign:
            self.broker.release(1, first.model_copy(update={"barrera": second.barrera}))
        self.assertEqual(foreign.exception.code, "different_gesture")
        self.assertGreater(self.broker.heartbeat(self.device, 2).vigencia_ms, 0)
        self.broker.release(1, second)
        assert_neutral(self, self.broker.heartbeat(self.device, 3))

    def test_replays_do_not_extend_lease(self):
        self.broker.acquire(1, self.vector)
        vector = self.vector.model_copy(update={"secuencia": 2})
        self.broker.update(1, vector)
        self.now += .3
        with self.assertRaises(ApplicationError) as replay:
            self.broker.update(1, vector)
        self.assertEqual(replay.exception.code, "stale_sequence")
        self.broker.heartbeat(self.device, 2)
        self.now += .451
        assert_neutral(self, self.broker.heartbeat(self.device, 3))

    def test_delayed_reconnect_uses_new_barrier_and_cannot_rearm(self):
        self.broker.acquire(1, self.vector)
        self.broker.update(1, self.vector.model_copy(update={"secuencia": 2}))
        replacement = self.broker.connect()
        self.assertFalse(self.broker.capability(1).disponible)
        with self.assertRaises(ValueError):
            self.broker.heartbeat(self.device, 2)
        self.broker.disconnect(self.device)
        assert_neutral(self, self.broker.heartbeat(replacement, 1))
        self.assertTrue(self.broker.capability(1).disponible)
        with self.assertRaises(ApplicationError):
            self.broker.acquire(1, self.vector)
        self.broker.disconnect(replacement)
        self.assertFalse(self.broker.capability(1).disponible)

    def test_demo_legacy_mutation_and_continuous_are_mutually_exclusive(self):
        demo = SimpleNamespace(id_ejecucion="demo-local")
        self.coordinator.reserve(1, demo)
        with self.assertRaises(ApplicationError):
            self.broker.acquire(1, self.vector)
        self.coordinator.clear_if_owner(1, demo.id_ejecucion)
        with self.coordinator.legacy_mutation(1):
            with self.assertRaises(ApplicationError):
                self.broker.acquire(1, self.vector)
        self.broker.acquire(1, self.vector)
        with self.assertRaises(ApplicationError):
            self.coordinator.reserve(1, demo)
        with self.assertRaises(ApplicationError):
            with self.coordinator.legacy_mutation(1):
                self.fail("la mutación legacy compitió con el permiso")
        self.coordinator.ensure_available(2)
        self.now += .751
        with self.coordinator.legacy_mutation(1):
            self.assertFalse(self.broker.capability(1).disponible)

    def test_private_environment_loads_without_exposing_the_device_token(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(f"IOT_CONTINUOUS_ENABLED=true\nIOT_CONTINUOUS_DEVICE_TOKEN={DEVICE_TOKEN}\n",
                                encoding="utf-8")
            isolated = DeviceSettings(_env_file=env_file)
        self.assertTrue(isolated.enabled)
        self.assertFalse(isolated.diagonals)
        self.assertTrue(isolated.authenticates("Bearer " + DEVICE_TOKEN))
        self.assertNotIn(DEVICE_TOKEN, repr(isolated))
        self.assertNotIn(DEVICE_TOKEN, isolated.model_dump_json())

    def test_pulse_keeps_its_id_across_heartbeats_and_expires_without_replay(self):
        ticket = self.broker.pulse_ticket(1, str(self.vector.barrera))
        self.assertTrue(self.broker.dispatch_pulse(1, ticket, 3))
        first = self.broker.heartbeat(self.device, 2)
        self.assertEqual((first.codigo, first.pulso, first.vigencia_ms), (3, 1, 250))
        self.now += .1
        continued = self.broker.heartbeat(self.device, 3)
        self.assertEqual((continued.codigo, continued.pulso), (3, 1))
        self.assertLess(continued.vigencia_ms, first.vigencia_ms)
        self.now += .16
        expired = self.broker.heartbeat(self.device, 4)
        self.assertEqual((expired.codigo, expired.pulso), (0, 0))
        self.assertTrue(self.broker.dispatch_pulse(1, ticket, 4, 1000))
        next_pulse = self.broker.heartbeat(self.device, 5)
        self.assertEqual((next_pulse.codigo, next_pulse.pulso, next_pulse.vigencia_ms), (4, 2, 750))
        replacement = self.broker.connect()
        self.assertFalse(self.broker.dispatch_pulse(1, ticket, 3))
        snapshot = self.broker.heartbeat(replacement, 1)
        self.assertEqual((snapshot.codigo, snapshot.pulso), (0, 0))


class StopPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_fence_precedes_blocked_snapshot(self):
        entered, leave = Event(), Event()
        data = Mock()

        def snapshot(_):
            entered.set()
            if not leave.wait(2):
                raise RuntimeError("snapshot del doble bloqueado")
            return None

        data.snapshot.side_effect = snapshot
        runtime = AppRuntime(data, device_settings=device_settings())
        runtime.bind_loop()
        broker = runtime.continuous
        device = broker.connect()
        broker.heartbeat(device, 1)
        vector = ControlVector(barrera=broker.capability(1).barrera, gesto=uuid4(),
                               secuencia=1, longitudinal=1, lateral=0)
        broker.acquire(1, vector)
        broker.update(1, vector.model_copy(update={"secuencia": 2}))
        legacy = Mock()
        facade = CoordinatedVehiculoService(runtime, legacy)
        with self.assertRaises(ApplicationError):
            facade.ejecutar_movimiento(1, object())
        legacy.ejecutar_movimiento.assert_not_called()
        stopping = asyncio.create_task(runtime.demos.stop(1))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, .5))
            self.assertFalse(stopping.done())
            assert_neutral(self, broker.heartbeat(device, 2))
            self.assertNotEqual(broker.capability(1).barrera, vector.barrera)
            with self.assertRaises(ApplicationError):
                broker.update(1, vector.model_copy(update={"secuencia": 3}))
        finally:
            leave.set()
            result = await asyncio.wait_for(stopping, 1)
            await runtime.shutdown()
        self.assertIs(result["detencion_logica"], True)


class ContinuousRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = Mock()
        self.data.snapshot.return_value = None
        self.runtime = AppRuntime(self.data, device_settings=device_settings())
        self.base = Mock()
        self.base.ejecutar_movimiento.side_effect = self.commit_movement
        self.runtime.bind_loop()
        self.addAsyncCleanup(self.runtime.shutdown)
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch("app.routes.continuous.runtime", self.runtime))
        stack.enter_context(patch("app.routes.websocket.runtime", self.runtime))
        stack.enter_context(patch("app.routes.demos.runtime", self.runtime))
        stack.enter_context(patch.dict(app.dependency_overrides, {
            get_service: lambda: CoordinatedVehiculoService(self.runtime, self.base),
            get_api_key_settings: lambda: ApiKeySettings(
                _env_file=None, api_key_operator=OPERATOR, api_key_admin="fixture-local-admin",
            ),
        }))

    @staticmethod
    def commit_movement(id_vehiculo, payload, *, before_execute):
        # El identificador de catálogo no coincide con el código cableado.
        before_execute(4)
        return MovimientoConfirmado.model_validate({**MOVIMIENTO, "estado": {**MOVIMIENTO["estado"], "codigo": 4}})

    async def managed(self, path, barrier, payload=None):
        headers = {"X-API-Key": OPERATOR}
        if barrier is not None:
            headers["X-IoT-Barrier"] = barrier
        return await cors_request("POST", path, headers=headers, payload=payload)

    def demo(self, *commands):
        plan = DemoPlan(1, 3, tuple(
            {"orden": index, "id_comando": index, "codigo": code, "duracion_ms": duration, "pausa_ms": pause}
            for index, (code, duration, pause) in enumerate(commands, 1)
        ))
        self.data.prepare.return_value = plan

        def step(_vehicle, _demo, movement, *, before_execute):
            before_execute(movement["codigo"])
            return {"id_comando": movement["id_comando"], "codigo": movement["codigo"]}

        self.data.step.side_effect = step
        return plan

    def demo_waits(self):
        waits = asyncio.Queue()

        async def wait(execution, milliseconds):
            if milliseconds:
                release = asyncio.Event()
                await waits.put((milliseconds, release))
                await asyncio.wait_for(release.wait(), 1)
            return execution.cancel_requested.is_set()

        self.enterContext(patch.object(self.runtime.demos, "_wait", side_effect=wait))
        return waits

    async def socket(self, path=DEVICE, headers=None):
        incoming, sent = asyncio.Queue(), []
        await incoming.put({"type": "websocket.connect"})
        scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "client": ("127.0.0.1", 3333),
            "server": ("127.0.0.1", 8000), "subprotocols": [],
            "headers": headers if headers is not None else [(b"authorization", ("Bearer " + DEVICE_TOKEN).encode())],
        }

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(app(scope, incoming.get, send))

        async def disconnect():
            if not task.done():
                await incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 1)

        self.addAsyncCleanup(disconnect)
        await until(lambda: bool(sent) or task.done())
        return incoming, sent

    async def send_frame(self, socket, data):
        incoming, sent = socket
        previous = len(sent)
        await incoming.put({"type": "websocket.receive", "text": json.dumps(data)})
        await until(lambda: len(sent) > previous)
        return sent[-1]

    async def send_heartbeat(self, socket, challenge):
        message = await self.send_frame(socket, {"tipo": "latido", "reto": challenge})
        self.assertEqual(message["type"], "websocket.send")
        frame = json.loads(message["text"])
        self.assertEqual((frame["tipo"], frame["reto"], frame["secuencia"]), ("control", challenge, challenge))
        return frame

    async def test_device_is_private_and_cardinal_control_keeps_public_monitor_unchanged(self):
        status, capabilities, headers = await main_request("GET", CONTROL + "/capacidad")
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertTrue(capabilities["habilitado"])
        self.assertFalse(capabilities["disponible"])
        self.assertFalse((await main_request("GET", "/vehiculos/2/control/capacidad"))[1]["habilitado"])
        for path, headers in [(DEVICE, []), (DEVICE, [(b"x-api-key", OPERATOR.encode())]), ("/ws/dispositivos/2", None)]:
            _, sent = await self.socket(path, headers)
            self.assertEqual([(m["type"], m["code"]) for m in sent], [("websocket.close", 4401)])

        socket = await self.socket()
        self.assertEqual(socket[1][0]["type"], "websocket.accept")
        first = await self.send_heartbeat(socket, 1)
        self.assertEqual((first["longitudinal"], first["lateral"], first["vigencia_ms"]), (0, 0, 0))
        _, capabilities, _ = await main_request("GET", CONTROL + "/capacidad")
        self.assertTrue(capabilities["disponible"])
        self.assertFalse(capabilities["diagonales"])
        vector = {"barrera": capabilities["barrera"], "gesto": str(uuid4()), "secuencia": 1,
                  "longitudinal": -1, "lateral": 0}
        self.assertEqual((await main_request("POST", CONTROL + "/permiso", vector))[0], 401)
        diagonal = {**vector, "lateral": 1}
        self.assertEqual((await main_request("POST", CONTROL + "/permiso", diagonal, OPERATOR))[0], 409)
        status, permit, _ = await main_request("POST", CONTROL + "/permiso", vector, OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual(permit["gesto"], vector["gesto"])
        self.assertEqual((await self.send_heartbeat(socket, 2))["vigencia_ms"], 0)
        invalid = {**vector, "secuencia": True}
        self.assertEqual((await main_request("POST", CONTROL + "/vector", invalid, OPERATOR))[0], 422)
        renewed = {**vector, "secuencia": 2}
        self.assertEqual((await main_request("POST", CONTROL + "/vector", renewed, OPERATOR))[0], 200)
        active = await self.send_heartbeat(socket, 3)
        self.assertEqual((active["longitudinal"], active["lateral"]), (-1, 0))
        self.assertGreater(active["vigencia_ms"], 0)
        release = {k: vector[k] for k in ("barrera", "gesto")}
        status, released, _ = await main_request("POST", CONTROL + "/liberar", release, OPERATOR)
        self.assertEqual(status, 200)
        self.assertTrue(released["liberado"])
        self.assertEqual((await self.send_heartbeat(socket, 4))["vigencia_ms"], 0)
        self.assertEqual((await main_request("POST", CONTROL + "/vector", {**vector, "secuencia": 3}, OPERATOR))[0], 409)
        self.data.snapshot.assert_not_called()
        self.data.prepare.assert_not_called()

        _, monitor = await self.socket("/ws/vehiculos/1", [])
        await until(lambda: any(m["type"] == "websocket.send" for m in monitor))
        state = json.loads(next(m["text"] for m in monitor if m["type"] == "websocket.send"))
        self.assertEqual(state["tipo"], "estado_inicial")
        self.assertNotIn("barrera", state)
        self.assertNotIn("vigencia_ms", state)

    async def test_evaluation_authorization_does_not_expand_to_new_routes(self):
        capability = (await main_request("GET", CONTROL + "/capacidad"))[1]
        body = {"barrera": capability["barrera"], "gesto": str(uuid4()), "secuencia": 1,
                "longitudinal": 1, "lateral": 0}
        headers = {"Origin": "https://jack28-08.github.io", "Authorization": "Bearer fixture-evaluation"}
        for suffix in ("/permiso", "/vector", "/liberar"):
            payload = {k: body[k] for k in ("barrera", "gesto")} if suffix == "/liberar" else body
            code, _, _ = await cors_request("POST", CONTROL + suffix, payload=payload, headers=headers)
            self.assertEqual(code, 403)

    async def test_reused_challenge_and_bad_payload_close_equipment_channel(self):
        for invalid in ({"tipo": "latido", "reto": 1}, {"tipo": "mover", "reto": 2},
                        {"tipo": "latido", "reto": True}):
            socket = await self.socket()
            await self.send_heartbeat(socket, 1)
            closed = await self.send_frame(socket, invalid)
            self.assertEqual((closed["type"], closed["code"]), ("websocket.close", 1008))
            _, capability, _ = await main_request("GET", CONTROL + "/capacidad")
            self.assertFalse(capability["disponible"])

    async def test_individual_requires_current_header_and_dispatches_committed_catalog_code(self):
        socket = await self.socket()
        await self.send_heartbeat(socket, 1)
        capability = (await main_request("GET", CONTROL + "/capacidad"))[1]
        barrier = capability["barrera"]
        self.assertFalse(capability["demo_remota"])
        path = "/vehiculos/1/movimientos"
        body = {"id_comando": 2, "origen": "HTTP"}
        for supplied in (None, str(uuid4())):
            self.assertEqual((await self.managed(path, supplied, body))[0], 409)
        self.base.ejecutar_movimiento.assert_not_called()

        self.base.ejecutar_movimiento.side_effect = lambda *_args, before_execute: before_execute(6)
        self.assertEqual((await self.managed(path, barrier, body))[0], 409)
        first = await self.send_heartbeat(socket, 2)
        self.assertEqual((first["codigo"], first["pulso"]), (0, 0))
        self.base.ejecutar_movimiento.side_effect = self.commit_movement
        code, _, raw = await self.managed(path, barrier, body)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["historial"]["id_historial"], MOVIMIENTO["historial"]["id_historial"])
        frame = await self.send_heartbeat(socket, 3)
        self.assertEqual((frame["codigo"], frame["pulso"], frame["longitudinal"], frame["lateral"]), (4, 1, 0, 0))
        self.assertLessEqual(frame["vigencia_ms"], 750)

    async def test_stop_fences_pulse_while_individual_commit_is_in_flight(self):
        socket = await self.socket()
        await self.send_heartbeat(socket, 1)
        barrier = (await main_request("GET", CONTROL + "/capacidad"))[1]["barrera"]
        entered, release = Event(), Event()

        def blocked(*args, before_execute):
            before_execute(4)
            entered.set()
            if not release.wait(1):
                raise RuntimeError("commit del doble bloqueado")
            return self.commit_movement(*args, before_execute=lambda _: None)

        self.base.ejecutar_movimiento.side_effect = blocked
        movement = asyncio.create_task(self.managed("/vehiculos/1/movimientos", barrier,
                                                   {"id_comando": 2, "origen": "HTTP"}))
        stop = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, .5))
            stop = asyncio.create_task(main_request("POST", "/vehiculos/1/detener", api_key=OPERATOR))
            await until(lambda: self.runtime.coordinator.stopping(1))
            self.assertFalse(stop.done())
            frame = await self.send_heartbeat(socket, 2)
            self.assertEqual((frame["codigo"], frame["pulso"]), (0, 0))
            self.assertEqual((await self.managed("/vehiculos/1/movimientos", barrier,
                                                {"id_comando": 2, "origen": "HTTP"}))[0], 409)
        finally:
            release.set()
            self.assertEqual((await asyncio.wait_for(movement, 1))[0], 200)
            if stop:
                self.assertEqual((await asyncio.wait_for(stop, 1))[0], 200)
        self.assertEqual((await self.send_heartbeat(socket, 3))["codigo"], 0)
        self.base.ejecutar_movimiento.assert_called_once()

    async def test_demo_uses_rds_durations_and_neutral_pause_with_strictly_increasing_pulses(self):
        self.demo((4, 1000, 150), (7, 650, 0))
        waits = self.demo_waits()
        socket = await self.socket()
        await self.send_heartbeat(socket, 1)
        barrier = (await main_request("GET", CONTROL + "/capacidad"))[1]["barrera"]
        path = "/vehiculos/1/demos/3/iniciar"
        self.assertEqual((await self.managed(path, None))[0], 409)
        self.data.prepare.assert_not_called()
        self.assertEqual((await self.managed(path, barrier))[0], 202)
        duration, first = await asyncio.wait_for(waits.get(), .5)
        self.assertEqual(duration, 1000)
        frame = await self.send_heartbeat(socket, 2)
        self.assertEqual((frame["codigo"], frame["pulso"], frame["vigencia_ms"]), (4, 1, 750))
        first.set()
        duration, pause = await asyncio.wait_for(waits.get(), .5)
        self.assertEqual(duration, 150)
        frame = await self.send_heartbeat(socket, 3)
        self.assertEqual((frame["codigo"], frame["pulso"], frame["vigencia_ms"]), (0, 0, 0))
        pause.set()
        duration, second = await asyncio.wait_for(waits.get(), .5)
        self.assertEqual(duration, 650)
        frame = await self.send_heartbeat(socket, 4)
        self.assertEqual((frame["codigo"], frame["pulso"]), (7, 2))
        self.assertLessEqual(frame["vigencia_ms"], 650)
        second.set()
        await until(lambda: self.runtime.coordinator.get_active(1) is None)
        self.assertNotEqual((await main_request("GET", CONTROL + "/capacidad"))[1]["barrera"], barrier)
        self.assertEqual((await self.send_heartbeat(socket, 5))["codigo"], 0)
        self.assertEqual(self.data.step.call_count, 2)
        self.base.ejecutar_movimiento.assert_not_called()

    async def test_cancel_cancels_acquisition_still_loading_its_plan(self):
        plan = self.demo((4, 700, 0))
        socket = await self.socket()
        await self.send_heartbeat(socket, 1)
        barrier = (await main_request("GET", CONTROL + "/capacidad"))[1]["barrera"]
        entered, release = Event(), Event()

        def prepare(*_args):
            entered.set()
            if not release.wait(1):
                raise RuntimeError("consulta del doble bloqueada")
            return plan

        self.data.prepare.side_effect = prepare
        starting = asyncio.create_task(self.managed("/vehiculos/1/demos/3/iniciar", barrier))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, .5))
            code, _, _ = await main_request("POST", "/vehiculos/1/demos/3/cancelar", api_key=OPERATOR)
            self.assertEqual(code, 409)  # No ejecución todavía. Se cercó igualmente un inicio atrasado.
        finally:
            release.set()
            self.assertEqual((await asyncio.wait_for(starting, 1))[0], 409)
        self.data.step.assert_not_called()
        self.assertEqual((await self.send_heartbeat(socket, 2))["codigo"], 0)

    async def test_cancel_does_not_wait_forever_for_reserved_demo_rejected_at_dispatch(self):
        self.demo((4, 700, 0))
        socket = await self.socket()
        await self.send_heartbeat(socket, 1)
        barrier = (await main_request("GET", CONTROL + "/capacidad"))[1]["barrera"]
        entered, release = Event(), Event()
        validate = self.runtime.continuous.validate_pulse_ticket

        def validate_at_dispatch(*args):
            if self.runtime.coordinator.get_active(1) is not None:
                entered.set()
                if not release.wait(1):
                    raise RuntimeError("despacho del doble bloqueado")
            return validate(*args)

        self.enterContext(patch.object(self.runtime.continuous, "validate_pulse_ticket", side_effect=validate_at_dispatch))
        starting = asyncio.create_task(self.managed("/vehiculos/1/demos/3/iniciar", barrier))
        cancelled = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, .5))
            execution = self.runtime.coordinator.get_active(1)
            cancelled = asyncio.create_task(main_request("POST", "/vehiculos/1/demos/3/cancelar", api_key=OPERATOR))
            await until(execution.cancel_requested.is_set)
        finally:
            release.set()
            self.assertEqual((await asyncio.wait_for(starting, 1))[0], 409)
            if cancelled:
                status, payload, _ = await asyncio.wait_for(cancelled, 1)
                self.assertEqual((status, payload["estado"]), (200, "cancelada"))
        self.data.step.assert_not_called()

    async def test_reconnect_during_demo_pause_cannot_rearm_or_confirm_finish(self):
        self.demo((1, 700, 150), (8, 700, 0))
        waits = self.demo_waits()
        socket = await self.socket()
        await self.send_heartbeat(socket, 1)
        _, monitor = await self.socket("/ws/vehiculos/1", [])
        barrier = (await main_request("GET", CONTROL + "/capacidad"))[1]["barrera"]
        self.assertEqual((await self.managed("/vehiculos/1/demos/3/iniciar", barrier))[0], 202)
        _, first = await asyncio.wait_for(waits.get(), .5)
        self.assertEqual((await self.send_heartbeat(socket, 2))["codigo"], 1)
        first.set()
        _, pause = await asyncio.wait_for(waits.get(), .5)
        restored = await self.socket()
        self.assertEqual((await self.send_heartbeat(restored, 1))["codigo"], 0)
        pause.set()
        await until(lambda: self.runtime.coordinator.get_active(1) is None)
        self.data.step.assert_called_once()
        messages = lambda: [json.loads(m["text"])["tipo"] for m in monitor if m["type"] == "websocket.send"]
        await until(lambda: "demo_error" in messages())
        self.assertNotIn("demo_fin", messages())
        self.assertEqual((await self.send_heartbeat(restored, 2))["codigo"], 0)


if __name__ == "__main__":
    unittest.main()
