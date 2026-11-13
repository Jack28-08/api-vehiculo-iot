"""Autorización de evaluación por ASGI real; servicios aislados, sin RDS."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.evaluation import EvaluationAccess, PAGES_ORIGIN
from app.routes.vehiculos import get_service
from app.main import app
from app.security import ApiKeySettings, get_api_key_settings
from tests.test_cors import cors_request
from tests.test_http import MOVIMIENTO

ISSUE = "/evaluacion/vehiculos/1"
MOVE = "/vehiculos/1/movimientos"
START = "/vehiculos/1/demos/3/iniciar"
CANCEL = "/vehiculos/1/demos/3/cancelar"
STOP = "/vehiculos/1/detener"
PAYLOAD = {"id_comando": 2, "origen": "CONTROL-WEB"}


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "evaluacion.json"
        self.now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        self.access = EvaluationAccess(self.path, now=lambda: self.now, ticks=lambda: self.now.timestamp())
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

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.overrides)

    def window(self, *, duration=30, delay=0):
        start = self.now + timedelta(minutes=delay)
        self.path.write_text(json.dumps({
            "id": str(uuid4()), "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(minutes=duration)).isoformat(),
        }), encoding="utf-8")

    async def call(self, method, path, *, token=None, origin=PAGES_ORIGIN, payload=None, key=None):
        headers = {"Origin": origin}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if key is not None:
            headers["X-API-Key"] = key
        return await cors_request(method, path, headers=headers, payload=payload)

    async def issue(self):
        code, headers, raw = await self.call("GET", ISSUE)
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
