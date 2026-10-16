"""Integración ASGI aislada de las rutas reales, autenticación y coordinación."""

import asyncio
import json
import threading
import unittest
from unittest.mock import Mock, patch

from app.database.connection import DatabaseUnavailableError
from app.errors import ApplicationError
from app.main import app
from app.models.vehiculos import MovimientoConfirmado
from app.routes.vehiculos import get_service
from app.runtime import AppRuntime, CoordinatedVehiculoService
from app.security import ApiKeySettings, get_api_key_settings
from tests.test_api_security import ADMIN_KEY, OPERATOR_KEY, WRITE_CASES, main_request
from tests.test_http import MOVIMIENTO
from tests.test_realtime import FakeData, Socket, until


class RealtimeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = FakeData(duration=60_000)
        self.runtime = AppRuntime(self.data)
        self.runtime.bind_loop()
        self.previous = app.dependency_overrides.copy()
        settings = ApiKeySettings(_env_file=None, api_key_operator=OPERATOR_KEY, api_key_admin=ADMIN_KEY)
        app.dependency_overrides[get_api_key_settings] = lambda: settings
        self.base = Mock()
        app.dependency_overrides[get_service] = lambda: CoordinatedVehiculoService(self.runtime, self.base)
        self.patches = [patch("app.routes.demos.runtime", self.runtime),
                        patch("app.routes.websocket.runtime", self.runtime)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        self.data.release.set()
        await self.runtime.shutdown()
        for p in self.patches:
            p.stop()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous)

    async def test_new_posts_reject_missing_or_invalid_keys_and_document_security(self):
        paths = ["/vehiculos/1/demos/31/iniciar", "/vehiculos/1/demos/31/cancelar", "/vehiculos/1/detener"]
        for path in paths:
            for key in (None, "invalid-key"):
                response = await main_request("POST", path, api_key=key)
                self.assertEqual(response[0], 401)
        self.assertEqual(self.data.calls, [])
        self.assertFalse(self.runtime.demos.tasks)
        schema = app.openapi()
        for path in ("/vehiculos/{id_vehiculo}/demos/{id_demo}/iniciar",
                     "/vehiculos/{id_vehiculo}/demos/{id_demo}/cancelar", "/vehiculos/{id_vehiculo}/detener"):
            self.assertTrue(schema["paths"][path]["post"]["security"])
        self.assertIn("202", schema["paths"]["/vehiculos/{id_vehiculo}/demos/{id_demo}/iniciar"]["post"]["responses"])

    async def test_operator_and_admin_start_cancel_stop_via_actual_http_routes(self):
        for key in (OPERATOR_KEY, ADMIN_KEY):
            code, start, _ = await main_request("POST", "/vehiculos/1/demos/31/iniciar", api_key=key)
            self.assertEqual(code, 202)
            self.assertEqual(start["total_movimientos"], 3)
            code, cancelled, _ = await main_request("POST", "/vehiculos/1/demos/31/cancelar", api_key=key)
            self.assertEqual((code, cancelled["estado"]), (200, "cancelada"))
            self.assertEqual(cancelled["id_ejecucion"], start["id_ejecucion"])
            code, stop, _ = await main_request("POST", "/vehiculos/1/detener", api_key=key)
            self.assertEqual(code, 200)
            self.assertTrue(stop["detencion_logica"])

    async def test_existing_mutations_reject_busy_vehicle_reads_and_other_vehicles_work(self):
        await asyncio.to_thread(self.runtime.demos.start, 1, 31)
        for path, _, payload, _, _ in WRITE_CASES[1:]:
            code, _, _ = await main_request("POST", path, payload, ADMIN_KEY)
            self.assertEqual(code, 409, path)
        self.base.assert_not_called()
        self.assertFalse(self.base.method_calls)
        self.base.obtener_historial.return_value = []
        self.assertEqual((await main_request("GET", "/vehiculos/1/historial"))[0], 200)
        self.base.eliminar_vehiculo.return_value = {"eliminado": True, "id": 2}
        self.assertEqual((await main_request("POST", "/vehiculos/2/eliminar", api_key=ADMIN_KEY))[0], 200)

    async def test_cancel_wrong_demo_and_no_active_are_safe(self):
        self.assertEqual((await main_request("POST", "/vehiculos/1/demos/31/cancelar", api_key=OPERATOR_KEY))[0], 409)
        await asyncio.to_thread(self.runtime.demos.start, 1, 31)
        self.assertEqual((await main_request("POST", "/vehiculos/1/demos/41/cancelar", api_key=OPERATOR_KEY))[0], 404)

    async def socket_session(self, identifier, inputs=None):
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        sent = []
        scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "ws", "path": f"/ws/vehiculos/{identifier}",
            "raw_path": f"/ws/vehiculos/{identifier}".encode(), "query_string": b"", "root_path": "",
            "headers": [], "client": ("127.0.0.1", 3333), "server": ("127.0.0.1", 8000),
            "subprotocols": [],
        }

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(app(scope, incoming.get, send))
        return task, incoming, sent

    async def test_ws_route_initial_state_client_commands_are_ignored_and_disconnect_cleans(self):
        task, incoming, sent = await self.socket_session(1)
        await until(lambda: any(m["type"] == "websocket.send" for m in sent))
        payload = json.loads(next(m["text"] for m in sent if m["type"] == "websocket.send"))
        self.assertEqual(payload["tipo"], "estado_inicial")
        self.assertIsNone(payload["conectado"])
        await incoming.put({"type": "websocket.receive", "text": '{"accion":"movimiento","id_comando":1}'})
        await asyncio.sleep(0.01)
        self.assertEqual(self.data.calls, [])
        self.assertFalse(self.runtime.demos.tasks)
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 1)
        self.assertFalse(self.runtime.connections.clients)

    async def test_ws_unknown_vehicle_or_rds_error_closes_safely(self):
        for error, code in ((ApplicationError(404, "Vehículo no encontrado."), 4404),
                            (DatabaseUnavailableError("network", "PRIVATE_RDS"), 1013)):
            with self.subTest(code=code):
                self.data.snapshot = Mock(side_effect=error)
                task, incoming, sent = await self.socket_session(1)
                await asyncio.wait_for(task, 1)
                self.assertEqual(sent[-1]["code"], code)
                self.assertNotIn("PRIVATE", json.dumps(sent))
                self.assertFalse(self.runtime.connections.clients)

    async def test_initial_snapshot_registration_and_later_commit_cannot_reorder(self):
        read_started, release = threading.Event(), threading.Event()

        def snapshot(identifier):
            read_started.set()
            if not release.wait(2):
                raise RuntimeError("test snapshot timeout")
            return {"id_comando": 1, "comando": "Anterior", "codigo": 1, "conectado": False}

        self.data.snapshot = snapshot
        monitor = Socket()
        registration = asyncio.create_task(asyncio.to_thread(self.runtime.subscribe, monitor, 1))
        await until(read_started.is_set)
        self.base.ejecutar_movimiento.return_value = MovimientoConfirmado.model_validate(MOVIMIENTO)
        facade = CoordinatedVehiculoService(self.runtime, self.base)
        movement = asyncio.create_task(asyncio.to_thread(facade.ejecutar_movimiento, 1, object()))
        await asyncio.sleep(0.01)
        self.assertFalse(movement.done())
        self.base.ejecutar_movimiento.assert_not_called()
        release.set()
        await registration
        await movement
        await until(lambda: len(monitor.sent) == 2)
        self.assertEqual([e["tipo"] for e in monitor.sent], ["estado_inicial", "movimiento_individual"])
        self.assertEqual([e["id_comando"] for e in monitor.sent], [1, 2])


if __name__ == "__main__":
    unittest.main()
