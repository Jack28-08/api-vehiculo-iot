"""Autorización de evaluación por ASGI real; servicios aislados, sin RDS."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.evaluation import DirectAccess, DirectAccessSettings, EvaluationAccess, PAGES_ORIGIN
from app.routes.vehiculos import get_service
from app.main import app
from app.security import ApiKeySettings, get_api_key_settings
from app.services.continuous import ContinuousControl, DeviceSettings
from app.services.coordination import VehicleCoordinator
from tests.test_cors import cors_request
from tests.test_http import MOVIMIENTO

ISSUE = "/evaluacion/vehiculos/1"
MOVE = "/vehiculos/1/movimientos"
START = "/vehiculos/1/demos/3/iniciar"
CANCEL = "/vehiculos/1/demos/3/cancelar"
STOP = "/vehiculos/1/detener"
CONTINUOUS = "/vehiculos/1/control"
PAYLOAD = {"id_comando": 2, "origen": "CONTROL-WEB"}


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "evaluacion.json"
        self.now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        self.access = EvaluationAccess(self.path, now=lambda: self.now, ticks=lambda: self.now.timestamp())
        self.direct_enabled = self.replace("app.evaluation.direct_enabled", return_value=False)
        for target in ("app.evaluation.evaluation_access", "app.security.evaluation_access"):
            fixture = patch(target, self.access)
            fixture.start()
            self.addCleanup(fixture.stop)
        database = patch("app.database.connection.mysql.connector.connect",
                         side_effect=AssertionError("RDS no permitido en esta prueba"))
        self.connect = database.start()
        self.addCleanup(database.stop)
        self.overrides = app.dependency_overrides.copy()
        self.addCleanup(self.restore_overrides)
        self.service = Mock()
        self.service.ejecutar_movimiento.return_value = MOVIMIENTO
        app.dependency_overrides[get_service] = lambda: self.service
        app.dependency_overrides[get_api_key_settings] = lambda: ApiKeySettings(
            _env_file=None, api_key_operator="test-isolated-operator", api_key_admin="test-isolated-admin",
        )
        execution = {"id_ejecucion": str(uuid4()), "id_vehiculo": 1, "id_demo": 3,
                     "total_movimientos": 2, "estado": "iniciada"}
        self.runtime = Mock()
        self.runtime.track_control.side_effect = lambda task: task
        self.runtime.demos.start.return_value = execution
        self.runtime.demos.cancel = AsyncMock(return_value={**execution, "estado": "cancelada"})
        self.runtime.demos.stop = AsyncMock(return_value={
            "id_vehiculo": 1, "id_ejecucion": None,
            "detencion_logica": True, "estado": "detenida_logicamente",
        })
        fixture = patch("app.routes.demos.runtime", self.runtime)
        fixture.start()
        self.addCleanup(fixture.stop)
        self.addCleanup(self.connect.assert_not_called)

    def replace(self, target, *args, **kwargs):
        fixture = patch(target, *args, **kwargs)
        self.addCleanup(fixture.stop)
        return fixture.start()

    def enable_direct(self, *, managed=True):
        self.direct_enabled.return_value = True
        access = DirectAccess(self.path, now=lambda: self.now, ticks=lambda: self.now.timestamp())
        self.replace("app.evaluation.direct_access", access)
        broker = ContinuousControl(
            VehicleCoordinator(clock=lambda: 100.0),
            DeviceSettings(_env_file=None, enabled=managed, device_token="device-test-key" * 4),
            clock=lambda: 100.0,
        )
        self.replace("app.evaluation.runtime", self.runtime)
        self.replace("app.routes.continuous.runtime", self.runtime)
        self.runtime.continuous = broker
        if managed:
            broker.heartbeat(broker.connect(), 1)
        return broker

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.overrides)

    def window(self, *, duration=30, delay=0):
        start = self.now + timedelta(minutes=delay)
        self.path.write_text(json.dumps({
            "id": str(uuid4()), "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(minutes=duration)).isoformat(),
        }), encoding="utf-8")

    async def call(self, method, path, *, token=None, origin=PAGES_ORIGIN, payload=None,
                   key=None, client="127.0.0.1", barrier=None):
        headers = {"Origin": origin}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if key is not None:
            headers["X-API-Key"] = key
        if barrier is not None:
            headers["X-IoT-Barrier"] = barrier
        return await cors_request(method, path, headers=headers, payload=payload, client=client)

    async def connect_device(self, token):
        path = "/ws/dispositivos/1"
        scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "client": ("127.0.0.1", 3333),
            "server": ("127.0.0.1", 8000), "subprotocols": [],
            "headers": [(b"authorization", ("Bearer " + token).encode())],
        }
        incoming = iter(({"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}))
        sent = []

        async def receive():
            return next(incoming)

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        return sent

    async def issue(self, *, client="127.0.0.1"):
        code, headers, raw = await self.call("GET", ISSUE, client=client)
        self.assertEqual(code, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        return json.loads(raw)

    async def test_disabled_future_invalid_and_overlong_fail_closed(self):
        self.assertEqual((await self.call("GET", ISSUE))[0], 403)
        self.window(delay=1)
        self.assertEqual((await self.call("GET", ISSUE))[0], 403)
        self.window(duration=121)
        self.assertEqual((await self.call("GET", ISSUE))[0], 403)
        self.path.write_text("no-json", encoding="utf-8")
        self.assertEqual((await self.call("GET", ISSUE))[0], 403)

    async def test_all_four_routes_use_real_handlers_without_permanent_key(self):
        self.window()
        token = (await self.issue())["token"]
        for path, status, body in ((MOVE, 200, PAYLOAD), (START, 202, None),
                                   (CANCEL, 200, None), (STOP, 200, None)):
            with self.subTest(path=path):
                self.assertEqual((await self.call("POST", path, token=token, payload=body))[0], status)
        self.service.ejecutar_movimiento.assert_called_once()
        self.runtime.demos.start.assert_called_once_with(1, 3)
        self.runtime.demos.cancel.assert_awaited_once_with(1, 3)
        self.runtime.demos.stop.assert_awaited_once_with(1)

    async def test_scope_and_origin_do_not_grant_admin_or_other_vehicle(self):
        self.window()
        self.assertEqual((await self.call("GET", ISSUE, origin="http://localhost:5500"))[0], 403)
        token = (await self.issue())["token"]
        self.assertEqual((await self.call("POST", MOVE, token=token, origin="https://example.org", payload=PAYLOAD))[0], 403)
        self.assertEqual((await self.call("POST", "/vehiculos/2/movimientos", token=token, payload=PAYLOAD))[0], 403)
        self.assertEqual((await self.call("POST", "/vehiculos/1/comandos", token=token,
                                         payload={"nombre": "Ejemplo", "codigo": 2}))[0], 403)
        self.service.ejecutar_movimiento.assert_not_called()
        self.service.crear_comando.assert_not_called()

    async def test_expired_bearer_and_window_revocation(self):
        self.window()
        session = await self.issue()
        self.assertEqual(datetime.fromisoformat(session["expires_at"]), self.now + timedelta(minutes=10))
        self.assertEqual((await self.call("POST", MOVE, token="a" * 43, payload=PAYLOAD))[0], 401)
        self.now += timedelta(minutes=10)
        self.assertEqual((await self.call("POST", MOVE, token=session["token"], payload=PAYLOAD))[0], 401)
        token = (await self.issue())["token"]
        self.path.unlink()
        self.assertEqual((await self.call("POST", STOP, token=token))[0], 403)
        self.window()
        self.assertEqual((await self.call("POST", STOP, token=token))[0], 401)

    async def test_end_of_window_caps_session_and_old_api_key_remains_valid(self):
        self.window(duration=3)
        session = await self.issue()
        self.assertEqual(datetime.fromisoformat(session["expires_at"]), self.now + timedelta(minutes=3))
        self.now += timedelta(minutes=3)
        self.assertEqual((await self.call("POST", MOVE, token=session["token"], payload=PAYLOAD))[0], 403)
        self.assertEqual((await self.call("POST", MOVE, key="test-isolated-operator", payload=PAYLOAD))[0], 200)

    async def test_replacing_active_window_revokes_old_tokens_without_waiting_for_expiry(self):
        self.window()
        old_token = (await self.issue())["token"]
        self.window()
        self.assertEqual((await self.call("POST", STOP, token=old_token))[0], 401)
        self.runtime.demos.stop.assert_not_awaited()
        new_token = (await self.issue())["token"]
        self.assertEqual((await self.call("POST", STOP, token=new_token))[0], 200)

    async def test_session_is_bound_to_the_peer_and_cannot_be_reused_from_another_ip(self):
        self.window()
        token = (await self.issue())["token"]
        self.assertEqual((await self.call("POST", MOVE, token=token, payload=PAYLOAD,
                                         client="192.0.2.5"))[0], 401)
        self.service.ejecutar_movimiento.assert_not_called()
        self.assertEqual((await self.call("POST", MOVE, token=token, payload=PAYLOAD))[0], 200)

    async def test_move_rate_limit_leaves_independent_stop_and_cancel_capacity(self):
        self.window()
        token = (await self.issue())["token"]
        for _ in range(45):
            self.assertEqual((await self.call("POST", MOVE, token=token, payload=PAYLOAD))[0], 200)
        code, headers, _ = await self.call("POST", MOVE, token=token, payload=PAYLOAD)
        self.assertEqual(code, 429)
        self.assertEqual(headers["retry-after"], "60")
        self.assertEqual((await self.call("POST", STOP, token=token))[0], 200)
        self.assertEqual((await self.call("POST", CANCEL, token=token))[0], 200)
        self.now += timedelta(seconds=60)
        self.assertEqual((await self.call("POST", MOVE, token=token, payload=PAYLOAD))[0], 200)

    async def test_issue_rate_limit_and_authorization_preflight(self):
        self.window()
        for _ in range(8):
            await self.issue()
        self.assertEqual((await self.call("GET", ISSUE))[0], 429)
        status, headers, _ = await cors_request("OPTIONS", MOVE, headers={
            "Origin": PAGES_ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        })
        self.assertEqual(status, 200)
        self.assertIn("authorization", headers["access-control-allow-headers"].lower())

    async def test_global_move_limit_across_clients_preserves_stop_and_cancel_capacity(self):
        self.window()
        clients = ("192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4")
        tokens = [(await self.issue(client=client))["token"] for client in clients]
        for client, token in zip(clients[:3], tokens):
            for _ in range(40):
                status, _, _ = await self.call("POST", MOVE, token=token, payload=PAYLOAD, client=client)
                self.assertEqual(status, 200)
        client, token = clients[-1], tokens[-1]
        status, _, _ = await self.call("POST", MOVE, token=token, payload=PAYLOAD, client=client)
        self.assertEqual(status, 429)
        self.assertEqual(self.service.ejecutar_movimiento.call_count, 120)
        for path in (STOP, CANCEL):
            self.assertEqual((await self.call("POST", path, token=token, client=client))[0], 200)

    async def test_direct_without_window_and_renew_after_expiry(self):
        self.enable_direct()
        self.assertFalse(self.path.exists())
        first = await self.issue()
        other = await self.issue(client="192.0.2.9")
        self.assertEqual(first["modo"], "directo")
        self.assertNotEqual(first["token"], other["token"])
        self.assertEqual(datetime.fromisoformat(first["expires_at"]), self.now + timedelta(minutes=10))
        self.assertEqual((await self.call("POST", STOP, token=other["token"], client="192.0.2.9"))[0], 200)
        self.now += timedelta(minutes=10)
        self.assertEqual((await self.call("POST", STOP, token=first["token"]))[0], 401)
        fresh = await self.issue()
        self.assertEqual((await self.call("POST", STOP, token=fresh["token"]))[0], 200)

    async def test_direct_scope_covers_real_control_handlers_and_excludes_admin_and_device(self):
        broker = self.enable_direct()
        origin = "https://example.test"
        self.assertEqual((await self.call("GET", ISSUE, origin=origin))[0], 403)
        token = (await self.issue())["token"]
        for path, status, body in ((MOVE, 200, PAYLOAD), (START, 202, None),
                                   (CANCEL, 200, None), (STOP, 200, None)):
            with self.subTest(path=path):
                barrier = str(broker.capability(1).barrera) if path in (MOVE, START) else None
                self.assertEqual((await self.call("POST", path, token=token, payload=body, barrier=barrier))[0], status)
        gesture = {"barrera": str(broker.capability(1).barrera), "gesto": str(uuid4())}
        vector = {**gesture, "secuencia": 1, "longitudinal": 1, "lateral": 0}
        for action, body in (("permiso", vector), ("vector", {**vector, "secuencia": 2}),
                             ("liberar", gesture)):
            with self.subTest(action=action):
                self.assertEqual((await self.call("POST", f"{CONTINUOUS}/{action}", token=token, payload=body))[0], 200)
        for path, body in (("/vehiculos/1/comandos", {"nombre": "Otro", "codigo": 9}),
                           ("/vehiculos/2/movimientos", PAYLOAD),
                           ("/vehiculos/2/control/permiso", vector)):
            with self.subTest(path=path):
                self.assertEqual((await self.call("POST", path, token=token, payload=body))[0], 403)
        self.assertEqual((await self.call("POST", STOP, token=token, origin=origin))[0], 403)
        self.assertEqual((await self.call("POST", STOP, token=token, client="192.0.2.9"))[0], 401)
        self.service.crear_comando.assert_not_called()
        frames = await self.connect_device(token)
        self.assertEqual([(frame["type"], frame["code"]) for frame in frames], [("websocket.close", 4401)])

    async def test_direct_refuses_legacy_and_temporary_cannot_renew_continuous(self):
        self.enable_direct(managed=False)
        self.window()
        self.assertEqual((await self.call("GET", ISSUE))[0], 503)
        self.assertEqual((await self.call("POST", MOVE, token="a" * 43, payload=PAYLOAD))[0], 503)
        self.service.ejecutar_movimiento.assert_not_called()

        self.direct_enabled.return_value = False
        temporary = await self.issue()
        self.assertNotIn("modo", temporary)
        self.assertEqual((await self.call("POST", f"{CONTINUOUS}/vector",
                                         token=temporary["token"], payload={}))[0], 403)

    async def test_direct_restart_reloads_policy_and_revokes_old_ram_session(self):
        self.enable_direct()
        first = await self.issue()
        self.enable_direct()
        self.assertEqual((await self.call("POST", STOP, token=first["token"]))[0], 401)
        self.assertEqual((await self.call("POST", STOP, token=(await self.issue())["token"]))[0], 200)
        self.direct_enabled.return_value = False
        self.assertEqual((await self.call("GET", ISSUE))[0], 403)
        self.assertEqual((await self.call("POST", STOP, token=first["token"]))[0], 403)

    async def test_direct_rate_of_keepalive_has_separate_room_for_stop(self):
        broker = self.enable_direct()
        token = (await self.issue())["token"]
        gesture = {"barrera": str(broker.capability(1).barrera), "gesto": str(uuid4())}
        vector = {**gesture, "secuencia": 1, "longitudinal": 1, "lateral": 0}
        self.assertEqual((await self.call("POST", f"{CONTINUOUS}/permiso", token=token, payload=vector))[0], 200)
        for sequence in range(2, 302):
            code, _, _ = await self.call("POST", f"{CONTINUOUS}/vector", token=token,
                                         payload={**vector, "secuencia": sequence})
            self.assertEqual(code, 200)
        self.assertEqual((await self.call("POST", f"{CONTINUOUS}/liberar", token=token, payload=gesture))[0], 200)
        self.assertEqual((await self.call("POST", STOP, token=token))[0], 200)

    async def test_direct_setting_reads_dotenv_without_window_file(self):
        self.path.write_text("IOT_DIRECT_ENABLED=true\n", encoding="utf-8")
        with patch.dict("os.environ", clear=True):
            self.assertTrue(DirectAccessSettings(_env_file=self.path).enabled)
            self.assertTrue(DirectAccessSettings(_env_file=self.path).enabled)
