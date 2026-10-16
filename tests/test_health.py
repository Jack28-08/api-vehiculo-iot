"""Pruebas unitarias HTTP; los dobles de MySQL no validan una conexión real."""

import asyncio
import inspect
import json
import threading
import unittest
from unittest.mock import patch

from app.database.connection import DatabaseUnavailableError
from app.main import app
from app.routes.health import health_db


async def request(path: str) -> tuple[int, dict]:
    """Realiza una petición ASGI sin requerir un cliente HTTP adicional."""
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(body)


class HealthTests(unittest.TestCase):
    def test_api_health_does_not_access_database(self):
        with patch("app.routes.health.MySQLConnectionManager") as manager:
            self.assertEqual(asyncio.run(request("/health")), (200, {"status": "ok"}))
        manager.assert_not_called()

    def test_database_success_runs_outside_event_loop_thread(self):
        event_loop_thread = threading.get_ident()
        database_threads = []
        with patch("app.routes.health.MySQLConnectionManager") as manager:
            manager.return_value.check_connection.side_effect = lambda: database_threads.append(
                threading.get_ident()
            )
            self.assertEqual(asyncio.run(request("/health/db")), (200, {"status": "ok"}))
        manager.return_value.check_connection.assert_called_once_with()
        self.assertNotEqual(database_threads, [event_loop_thread])
        self.assertFalse(inspect.iscoroutinefunction(health_db))

    def test_database_failure_has_safe_http_response(self):
        failure = DatabaseUnavailableError("connection_failed", "SENSITIVE_INTERNAL_DIAGNOSTIC")
        with patch("app.routes.health.MySQLConnectionManager") as manager:
            manager.return_value.check_connection.side_effect = failure
            self.assertEqual(
                asyncio.run(request("/health/db")),
                (503, {"status": "unavailable", "message": "Base de datos no disponible."}),
            )

    def test_configuration_failure_has_safe_http_response(self):
        failure = DatabaseUnavailableError("configuration_invalid", "SENSITIVE_CONFIGURATION_DETAIL")
        with patch("app.routes.health.MySQLConnectionManager", side_effect=failure):
            self.assertEqual(
                asyncio.run(request("/health/db")),
                (503, {"status": "unavailable", "message": "Base de datos no disponible."}),
            )

    def test_openapi_documents_database_unavailable_response(self):
        responses = app.openapi()["paths"]["/health/db"]["get"]["responses"]
        self.assertIn("200", responses)
        self.assertEqual(
            responses["503"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DatabaseUnavailableResponse",
        )


if __name__ == "__main__":
    unittest.main()
