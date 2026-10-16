"""Permisos de las rutas reales y errores seguros; sin .env ni acceso a RDS."""

import asyncio
import json
import unittest
from unittest.mock import Mock

from app.database.connection import DatabaseUnavailableError
from app.errors import ApplicationError
from app.main import app
from app.routes.vehiculos import get_service
from app.security import ApiKeySettings, get_api_key_settings
from tests.test_http import COMANDO, DEMO, DEMO_MOVIMIENTO, MOVIMIENTO, VEHICULO


OPERATOR_KEY = "isolated-test-operator-key"
ADMIN_KEY = "isolated-test-admin-key"

# Path, método del servicio, cuerpo, respuesta y código HTTP esperado.
WRITE_CASES = [
    ("/vehiculos", "crear_vehiculo", {"nombre": "Auto"}, VEHICULO, 201),
    ("/vehiculos/1/actualizar", "actualizar_vehiculo", {"nombre": "Auto", "activo": True}, VEHICULO, 200),
    ("/vehiculos/1/eliminar", "eliminar_vehiculo", None, {"eliminado": True, "id": 1}, 200),
    ("/vehiculos/1/comandos", "crear_comando", {"nombre": "Avanzar", "codigo": 0}, COMANDO, 201),
    ("/vehiculos/1/comandos/2/actualizar", "actualizar_comando", {"nombre": "Avanzar", "codigo": 0, "activo": True}, COMANDO, 200),
    ("/vehiculos/1/comandos/2/eliminar", "eliminar_comando", None, {"eliminado": True, "id": 2}, 200),
    ("/vehiculos/1/demos", "crear_demo", {"nombre": "Secuencia"}, DEMO, 201),
    ("/vehiculos/1/demos/3/actualizar", "actualizar_demo", {"nombre": "Secuencia", "activo": True}, DEMO, 200),
    ("/vehiculos/1/demos/3/eliminar", "eliminar_demo", None, {"eliminado": True, "id": 3}, 200),
    ("/vehiculos/1/demos/3/movimientos", "crear_movimiento_demo", {"id_comando": 2, "orden": 1, "duracion_ms": 1}, DEMO_MOVIMIENTO, 201),
    ("/vehiculos/1/demos/3/movimientos/5/actualizar", "actualizar_movimiento_demo", {"id_comando": 2, "orden": 1, "duracion_ms": 1}, DEMO_MOVIMIENTO, 200),
    ("/vehiculos/1/demos/3/movimientos/5/eliminar", "eliminar_movimiento_demo", None, {"eliminado": True, "id": 5}, 200),
    ("/vehiculos/1/movimientos", "ejecutar_movimiento", {"id_comando": 2, "origen": "HTTP"}, MOVIMIENTO, 200),
]


async def main_request(method, path, payload=None, api_key=None):
    messages = []
    raw = json.dumps(payload).encode() if payload is not None else b""
    headers = [(b"content-type", b"application/json")]
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode("latin-1")))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": headers, "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        messages.append(message)

    try:
        await app(scope, receive, send)
    except Exception:
        # ServerErrorMiddleware vuelve a lanzar el error después de enviar 500.
        # Solo se acepta si el handler ya produjo esa respuesta real.
        if not any(m["type"] == "http.response.start" and m["status"] == 500 for m in messages):
            raise
    response = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return response["status"], json.loads(body), dict(response["headers"])


class RealRouteSecurityTests(unittest.TestCase):
    def setUp(self):
        self.previous_overrides = app.dependency_overrides.copy()
        self.settings = ApiKeySettings(
            api_key_operator=OPERATOR_KEY, api_key_admin=ADMIN_KEY, _env_file=None,
        )
        self.service = Mock()
        self.service_factory = Mock(return_value=self.service)
        app.dependency_overrides[get_api_key_settings] = lambda: self.settings
        app.dependency_overrides[get_service] = lambda: self.service_factory()
        # Evita que una prueba anterior pueda quitar la autenticación real.
        from app.security import get_current_role, require_admin, require_operator
        for dependency in (get_current_role, require_admin, require_operator):
            app.dependency_overrides.pop(dependency, None)

    def tearDown(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous_overrides)

    def call(self, method, path, payload=None, key=None):
        return asyncio.run(main_request(method, path, payload, key))

    def test_absent_key_rejects_all_thirteen_real_posts_before_service(self):
        for path, _, payload, _, _ in WRITE_CASES:
            with self.subTest(path=path):
                code, body, headers = self.call("POST", path, payload)
                self.assertEqual((code, body), (401, {"detail": "Clave API ausente o inválida."}))
                self.assertEqual(headers[b"www-authenticate"], b"ApiKey")
        self.service_factory.assert_not_called()

    def test_invalid_key_rejects_all_thirteen_real_posts_before_service(self):
        for path, _, payload, _, _ in WRITE_CASES:
            with self.subTest(path=path):
                code, body, _ = self.call("POST", path, payload, "unrecognized-test-key")
                self.assertEqual((code, body), (401, {"detail": "Clave API ausente o inválida."}))
        self.service_factory.assert_not_called()

    def test_operator_cannot_call_any_of_twelve_real_management_posts(self):
        for path, _, payload, _, _ in WRITE_CASES[:-1]:
            with self.subTest(path=path):
                code, body, _ = self.call("POST", path, payload, OPERATOR_KEY)
                self.assertEqual((code, body), (403, {"detail": "Permisos insuficientes."}))
        self.service_factory.assert_not_called()

    def test_operator_can_execute_real_movement_route(self):
        path, method, payload, result, _ = WRITE_CASES[-1]
        getattr(self.service, method).return_value = result
        code, body, _ = self.call("POST", path, payload, OPERATOR_KEY)
        self.assertEqual(code, 200)
        self.assertFalse(body["conectado"])
        self.service.ejecutar_movimiento.assert_called_once()

    def test_admin_can_call_all_thirteen_real_posts(self):
        for path, method, payload, result, expected in WRITE_CASES:
            with self.subTest(path=path):
                getattr(self.service, method).return_value = result
                code, _, _ = self.call("POST", path, payload, ADMIN_KEY)
                self.assertEqual(code, expected)
                getattr(self.service, method).assert_called_once()

    def test_database_errors_map_to_safe_http_statuses(self):
        for category, expected in [
            ("conflict", 409), ("validation", 422), ("query", 500),
            ("permission", 500), ("network", 503), ("configuration", 503),
        ]:
            with self.subTest(category=category):
                self.service.listar_vehiculos.side_effect = DatabaseUnavailableError(category, "SENSITIVE_TEST_DIAGNOSTIC")
                code, body, _ = self.call("GET", "/vehiculos")
                self.assertEqual(code, expected)
                self.assertNotIn("SENSITIVE_TEST_DIAGNOSTIC", json.dumps(body))
                self.assertNotIn(ADMIN_KEY, json.dumps(body))
                self.assertNotIn(OPERATOR_KEY, json.dumps(body))

    def test_domain_and_unexpected_error_handlers(self):
        self.service.listar_vehiculos.side_effect = ApplicationError(404, "Vehículo no encontrado.")
        self.assertEqual(self.call("GET", "/vehiculos")[:2], (404, {"detail": "Vehículo no encontrado."}))
        self.service.listar_vehiculos.side_effect = RuntimeError("SENSITIVE_TEST_DIAGNOSTIC")
        self.assertEqual(self.call("GET", "/vehiculos")[:2], (500, {"detail": "Error interno de la API."}))

    def test_validation_never_reflects_submitted_sensitive_value(self):
        marker = "SENSITIVE_TEST_MARKER_" * 100
        code, body, _ = self.call("POST", "/vehiculos", {"nombre": marker}, ADMIN_KEY)
        self.assertEqual(code, 422)
        self.assertNotIn(marker, json.dumps(body))
        self.assertTrue(all(set(item) == {"loc", "type", "msg"} for item in body["detail"]))
        self.service.crear_vehiculo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
