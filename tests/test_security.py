"""Autenticación ASGI y generación de claves con archivos temporales aislados."""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from typing import Annotated
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from fastapi import Depends, FastAPI

from app.security import (
    ApiKeySettings,
    Role,
    get_api_key_settings,
    get_current_role,
    require_admin,
    require_operator,
)
from scripts.ensure_api_keys import API_KEY_NAMES, ensure_api_keys, main


def create_test_app(operator="operator-test-secret", admin="admin-test-secret"):
    """No carga .env real y no importa la aplicación que consulta MySQL."""

    app = FastAPI()
    settings = ApiKeySettings(
        api_key_operator=operator,
        api_key_admin=admin,
        _env_file=None,
    )
    app.dependency_overrides[get_api_key_settings] = lambda: settings

    @app.post("/operator")
    def operator_endpoint(role: Annotated[Role, Depends(require_operator)]):
        return {"role": role}

    @app.post("/admin")
    def admin_endpoint(role: Annotated[Role, Depends(require_admin)]):
        return {"role": role}

    return app


async def request(app, path, api_key=None):
    """Realiza una petición ASGI sin dependencias HTTP adicionales."""

    messages = []
    headers = [] if api_key is None else [(b"x-api-key", api_key.encode("latin-1"))]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    response = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return response["status"], json.loads(body), dict(response["headers"])


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_test_app()

    def test_absent_and_invalid_key_return_same_safe_401(self):
        for value in (None, "", "unrecognized-key", "inválida"):
            with self.subTest(value=value):
                status, body, headers = asyncio.run(request(self.app, "/operator", value))
                self.assertEqual(status, 401)
                self.assertEqual(body, {"detail": "Clave API ausente o inválida."})
                self.assertEqual(headers[b"www-authenticate"], b"ApiKey")

    def test_operator_can_execute_movements(self):
        status, body, _ = asyncio.run(request(self.app, "/operator", "operator-test-secret"))
        self.assertEqual((status, body), (200, {"role": "operator"}))

    def test_operator_cannot_manage_resources(self):
        status, body, _ = asyncio.run(request(self.app, "/admin", "operator-test-secret"))
        self.assertEqual((status, body), (403, {"detail": "Permisos insuficientes."}))

    def test_admin_can_manage_resources_and_execute_movements(self):
        for path in ("/admin", "/operator"):
            with self.subTest(path=path):
                status, body, _ = asyncio.run(request(self.app, path, "admin-test-secret"))
                self.assertEqual((status, body), (200, {"role": "admin"}))

    def test_missing_blank_or_identical_configuration_fails_closed(self):
        for operator, admin in (
            ("", "admin-test-secret"),
            ("operator-test-secret", ""),
            ("   ", "admin-test-secret"),
            ("same-test-secret", "same-test-secret"),
        ):
            with self.subTest(operator=operator, admin=admin):
                app = create_test_app(operator, admin)
                status, body, _ = asyncio.run(request(app, "/admin", admin))
                self.assertEqual((status, body), (503, {"detail": "Autenticación no disponible."}))

    def test_unicode_keys_are_compared_as_utf8_bytes(self):
        settings = ApiKeySettings(
            api_key_operator="operador-prueba-🔑",
            api_key_admin="administrador-prueba-🔒",
            _env_file=None,
        )
        self.assertEqual(get_current_role("operador-prueba-🔑", settings), Role.OPERATOR)
        self.assertEqual(get_current_role("administrador-prueba-🔒", settings), Role.ADMIN)

    def test_settings_repr_does_not_include_api_keys(self):
        settings = ApiKeySettings(
            api_key_operator="operator-test-secret",
            api_key_admin="admin-test-secret",
            _env_file=None,
        )
        self.assertNotIn("operator-test-secret", repr(settings))
        self.assertNotIn("admin-test-secret", repr(settings))

    def test_swagger_exposes_header_authentication_for_both_roles(self):
        schema = self.app.openapi()
        self.assertEqual(
            schema["components"]["securitySchemes"]["ApiKey"]["name"], "X-API-Key"
        )
        self.assertEqual(schema["components"]["securitySchemes"]["ApiKey"]["in"], "header")
        for path in ("/admin", "/operator"):
            self.assertEqual(schema["paths"][path]["post"]["security"], [{"ApiKey": []}])


class ApiKeyGenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"

    def test_missing_keys_are_secure_random_distinct_and_not_printed(self):
        output = StringIO()
        with redirect_stdout(output):
            names = ensure_api_keys(self.path, environ={})
        values = dotenv_values(self.path, interpolate=False)
        self.assertEqual(names, list(API_KEY_NAMES))
        self.assertGreaterEqual(len(values["API_KEY_OPERATOR"]), 43)
        self.assertGreaterEqual(len(values["API_KEY_ADMIN"]), 43)
        self.assertNotEqual(values["API_KEY_OPERATOR"], values["API_KEY_ADMIN"])
        self.assertEqual(output.getvalue(), "")

    def test_preserves_all_existing_bytes_and_nonempty_keys(self):
        original = b'# Existing local settings\r\nMYSQL_DATABASE=vehiculo_iot\r\nAPI_KEY_ADMIN="kept-admin-test"'
        self.path.write_bytes(original)
        names = ensure_api_keys(self.path, environ={})
        self.assertEqual(names, ["API_KEY_OPERATOR"])
        self.assertTrue(self.path.read_bytes().startswith(original + b"\r\n"))
        self.assertEqual(dotenv_values(self.path)["API_KEY_ADMIN"], "kept-admin-test")

    def test_empty_file_value_gets_final_definition_without_rewrite(self):
        original = b"API_KEY_OPERATOR=\nAPI_KEY_ADMIN=kept-admin-test\n"
        self.path.write_bytes(original)
        self.assertEqual(ensure_api_keys(self.path, environ={}), ["API_KEY_OPERATOR"])
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertTrue(dotenv_values(self.path)["API_KEY_OPERATOR"])

    def test_nonempty_environment_keys_are_not_copied_into_file(self):
        environment = {
            "API_KEY_OPERATOR": "process-operator-test",
            "API_KEY_ADMIN": "process-admin-test",
        }
        self.assertEqual(ensure_api_keys(self.path, environ=environment), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(environment["API_KEY_OPERATOR"], "process-operator-test")

    def test_repeated_execution_does_not_rotate_existing_keys(self):
        ensure_api_keys(self.path, environ={})
        original = self.path.read_bytes()
        self.assertEqual(ensure_api_keys(self.path, environ={}), [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_generation_avoids_duplicate_keys(self):
        with patch(
            "scripts.ensure_api_keys.secrets.token_urlsafe",
            side_effect=["first-test-key", "first-test-key", "second-test-key"],
        ):
            ensure_api_keys(self.path, environ={})
        values = dotenv_values(self.path)
        self.assertNotEqual(values["API_KEY_OPERATOR"], values["API_KEY_ADMIN"])

    def test_empty_process_variable_does_not_replace_nonempty_file_value(self):
        original = b"API_KEY_OPERATOR=kept-operator-test\nAPI_KEY_ADMIN=kept-admin-test\n"
        self.path.write_bytes(original)
        self.assertEqual(ensure_api_keys(self.path, environ={"API_KEY_OPERATOR": ""}), [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_main_reports_empty_environment_without_showing_values(self):
        output = StringIO()
        with (
            patch("scripts.ensure_api_keys.ensure_api_keys", return_value=[]),
            patch.dict("os.environ", {"API_KEY_OPERATOR": ""}, clear=True),
            redirect_stdout(output),
        ):
            self.assertEqual(main(), 1)
        self.assertIn("API_KEY_OPERATOR", output.getvalue())
        self.assertIn("ocultan .env", output.getvalue())


if __name__ == "__main__":
    unittest.main()
