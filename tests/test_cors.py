"""CORS de la aplicación real mediante ASGI local, sin red, claves reales ni RDS."""

import json
import unittest
from unittest.mock import Mock, patch

from app.database.connection import DatabaseUnavailableError
from app.main import app
from app.routes.vehiculos import get_service
from app.security import ApiKeySettings, get_api_key_settings
from tests.test_api_security import ADMIN_KEY, OPERATOR_KEY
from tests.test_http import MOVIMIENTO


ALLOWED_ORIGINS = ("https://jack28-08.github.io", "http://localhost:5500")
UNAUTHORIZED_ORIGINS = (
    "https://example.com",
    "https://jack28-08.github.io.example.com",
    "http://jack28-08.github.io",
    "https://jack28-08.github.io/control-iot-jack/",
    "http://localhost:5501",
    "http://127.0.0.1:5500",
    "null",
)


async def cors_request(method, path, *, headers=None, payload=None, client="127.0.0.1"):
    """Invoca ASGI en memoria; incluso los POST de prueba no salen del proceso."""
    messages = []
    body = json.dumps(payload).encode() if payload is not None else b""
    request_headers = {"content-type": "application/json", **(headers or {})}
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(name.lower().encode("ascii"), value.encode("latin-1"))
                    for name, value in request_headers.items()],
        "client": (client, 12345), "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    response = next(m for m in messages if m["type"] == "http.response.start")
    response_headers = {name.decode("latin-1"): value.decode("latin-1")
                        for name, value in response["headers"]}
    response_body = b"".join(m.get("body", b"") for m in messages
                             if m["type"] == "http.response.body")
    return response["status"], response_headers, response_body


class CorsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_overrides = app.dependency_overrides.copy()
        self.settings = ApiKeySettings(
            _env_file=None, api_key_operator=OPERATOR_KEY, api_key_admin=ADMIN_KEY,
        )
        self.auth_settings = Mock(return_value=self.settings)
        self.service = Mock()
        self.service_factory = Mock(return_value=self.service)
        app.dependency_overrides[get_api_key_settings] = lambda: self.auth_settings()
        app.dependency_overrides[get_service] = lambda: self.service_factory()
        self.database_guard = patch(
            "app.database.connection.mysql.connector.connect",
            side_effect=AssertionError("Las pruebas CORS no deben conectar a MySQL."),
        )
        self.database_connect = self.database_guard.start()
        self.addCleanup(self.database_guard.stop)
        self.addCleanup(self.restore_overrides)

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous_overrides)

    def assert_origin(self, headers, origin):
        self.assertEqual(headers["access-control-allow-origin"], origin)
        self.assertIn("origin", {item.strip().lower() for item in headers["vary"].split(",")})
        self.assertNotIn("access-control-allow-credentials", headers)

    async def preflight(self, origin, *, method="POST", requested_headers=None):
        return await cors_request("OPTIONS", "/vehiculos/1/movimientos", headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": requested_headers or "X-API-Key, Content-Type, Accept",
        })

    async def test_allowed_origins_can_read_health_without_database(self):
        for origin in ALLOWED_ORIGINS:
            with self.subTest(origin=origin):
                status, headers, body = await cors_request("GET", "/health", headers={"Origin": origin})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"status": "ok"})
                self.assert_origin(headers, origin)
        self.database_connect.assert_not_called()

    async def test_non_cors_requests_preserve_the_existing_health_contract(self):
        status, headers, body = await cors_request("GET", "/health")
        self.assertEqual((status, json.loads(body)), (200, {"status": "ok"}))
        self.assertNotIn("access-control-allow-origin", headers)

    async def test_other_origins_are_not_granted_browser_access(self):
        for origin in UNAUTHORIZED_ORIGINS:
            with self.subTest(origin=origin):
                status, headers, _ = await cors_request("GET", "/health", headers={"Origin": origin})
                # CORS no convierte los GET públicos en rutas autenticadas.
                self.assertEqual(status, 200)
                self.assertNotIn("access-control-allow-origin", headers)

    async def test_allowed_preflights_do_not_invoke_authentication_or_commands(self):
        for origin in ALLOWED_ORIGINS:
            for method in ("GET", "POST", "OPTIONS"):
                with self.subTest(origin=origin, method=method):
                    status, headers, _ = await self.preflight(origin, method=method)
                    self.assertEqual(status, 200)
                    self.assert_origin(headers, origin)
                    self.assertEqual(
                        {item.strip() for item in headers["access-control-allow-methods"].split(",")},
                        {"GET", "POST", "OPTIONS"},
                    )
                    allowed_headers = {item.strip().lower()
                                       for item in headers["access-control-allow-headers"].split(",")}
                    self.assertTrue({"x-api-key", "content-type", "accept"} <= allowed_headers)
                    self.assertNotIn("*", allowed_headers)
        self.auth_settings.assert_not_called()
        self.service_factory.assert_not_called()
        self.database_connect.assert_not_called()

    async def test_preflights_reject_other_origins(self):
        for origin in UNAUTHORIZED_ORIGINS:
            with self.subTest(origin=origin):
                status, headers, _ = await self.preflight(origin)
                self.assertEqual(status, 400)
                self.assertNotIn("access-control-allow-origin", headers)
        self.service_factory.assert_not_called()

    async def test_preflights_reject_methods_outside_the_frontend_contract(self):
        for method in ("PUT", "PATCH", "DELETE", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual((await self.preflight(ALLOWED_ORIGINS[0], method=method))[0], 400)
        self.service_factory.assert_not_called()

    async def test_preflights_reject_unapproved_headers(self):
        for header in ("X-Admin-Key", "X-Requested-With"):
            with self.subTest(header=header):
                self.assertEqual((await self.preflight(
                    ALLOWED_ORIGINS[0], requested_headers=f"X-API-Key, {header}",
                ))[0], 400)
        self.service_factory.assert_not_called()

    async def test_preflight_header_names_are_case_insensitive(self):
        status, headers, _ = await self.preflight(
            ALLOWED_ORIGINS[1], requested_headers="x-api-key, CONTENT-TYPE, aCcEpT",
        )
        self.assertEqual(status, 200)
        self.assert_origin(headers, ALLOWED_ORIGINS[1])

    async def test_cors_does_not_bypass_post_authentication(self):
        for origin in ALLOWED_ORIGINS:
            with self.subTest(origin=origin):
                status, headers, _ = await cors_request(
                    "POST", "/vehiculos/1/movimientos", headers={"Origin": origin},
                    payload={"id_comando": 2, "origen": "CONTROL-WEB"},
                )
                self.assertEqual(status, 401)
                self.assert_origin(headers, origin)
        self.service_factory.assert_not_called()
        self.database_connect.assert_not_called()

    async def test_authorized_post_uses_existing_service_and_response(self):
        self.service.ejecutar_movimiento.return_value = MOVIMIENTO
        status, headers, body = await cors_request(
            "POST", "/vehiculos/1/movimientos",
            headers={"Origin": ALLOWED_ORIGINS[0], "X-API-Key": OPERATOR_KEY},
            payload={"id_comando": 2, "origen": "CONTROL-WEB"},
        )
        self.assertEqual(status, 200)
        self.assert_origin(headers, ALLOWED_ORIGINS[0])
        self.assertEqual(json.loads(body)["id_comando"], 2)
        self.service.ejecutar_movimiento.assert_called_once()
        self.database_connect.assert_not_called()

    async def test_handled_errors_keep_cors_headers_and_their_original_status(self):
        for code, expected in (("network", 503), ("query", 500)):
            with self.subTest(error=code):
                self.service.listar_vehiculos.side_effect = DatabaseUnavailableError(code)
                status, headers, _ = await cors_request(
                    "GET", "/vehiculos", headers={"Origin": ALLOWED_ORIGINS[0]},
                )
                self.assertEqual(status, expected)
                self.assert_origin(headers, ALLOWED_ORIGINS[0])

    async def test_missing_routes_and_validation_errors_keep_cors_headers(self):
        for path, expected in (("/ruta-inexistente", 404), ("/vehiculos/0", 422)):
            with self.subTest(path=path):
                status, headers, _ = await cors_request(
                    "GET", path, headers={"Origin": ALLOWED_ORIGINS[0]},
                )
                self.assertEqual(status, expected)
                self.assert_origin(headers, ALLOWED_ORIGINS[0])
        self.service.obtener_vehiculo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
