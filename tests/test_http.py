"""Pruebas HTTP aisladas: no abren conexiones ni escriben en Amazon RDS."""

import asyncio
import inspect
import json
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import Mock

from fastapi import FastAPI

from app.models.vehiculos import (
    ComandoCrear,
    DemoMovimientoCrear,
    VehiculoCrear,
)
from app.routes.vehiculos import get_service, router
from app.security import require_admin, require_operator


VEHICULO = {
    "id_vehiculo": 1, "nombre": "Vehículo de prueba", "modelo": None,
    "descripcion": None, "microcontrolador": None,
    "fecha_registro": "2026-10-15T12:00:00", "activo": 1,
}
COMANDO = {
    "id_comando": 2, "id_vehiculo": 1, "nombre": "Avanzar", "codigo": -1,
    "descripcion": None, "activo": 1,
}
DEMO = {"id_demo": 3, "id_vehiculo": 1, "nombre": "Secuencia", "descripcion": None, "activo": 1}
ESTADO = {
    "id_vehiculo": 1, "vehiculo": VEHICULO["nombre"], "id_comando": 2,
    "comando": "Avanzar", "codigo": -1, "conectado": 0,
    "fecha_actualizacion": "2026-10-15T12:00:00",
}
HISTORIAL = {
    "id_historial": 4, "id_vehiculo": 1, "comando": "Avanzar", "codigo": -1,
    "fecha_hora": "2026-10-15T12:00:00", "origen": "HTTP",
}
DEMO_MOVIMIENTO = {
    "id_demo_movimiento": 5, "orden": 1, "id_comando": 2, "comando": "Avanzar",
    "codigo": -1, "duracion_ms": 1000, "pausa_ms": 0,
}
MOVIMIENTO = {
    "id_vehiculo": 1, "id_comando": 2, "origen": "HTTP", "conectado": False,
    "estado": ESTADO, "historial": HISTORIAL,
}


async def request(app, method, path, payload=None):
    """Cliente ASGI mínimo; evita agregar dependencias solo para las pruebas."""
    messages = []
    body = json.dumps(payload).encode() if payload is not None else b""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status_code = next(m["status"] for m in messages if m["type"] == "http.response.start")
    response_body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status_code, json.loads(response_body)


class HTTPContractTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_service] = lambda: self.service
        # La autenticación real tiene su propia suite. Aquí se aísla el contrato.
        self.app.dependency_overrides[require_admin] = lambda: None
        self.app.dependency_overrides[require_operator] = lambda: None

    def call(self, method, path, payload=None):
        return asyncio.run(request(self.app, method, path, payload))

    def test_all_seven_queries_delegate_to_service(self):
        cases = [
            ("/vehiculos", "listar_vehiculos", (), [VEHICULO]),
            ("/vehiculos/1", "obtener_vehiculo", (1,), VEHICULO),
            ("/vehiculos/1/comandos", "obtener_comandos", (1,), [COMANDO]),
            ("/vehiculos/1/estado", "obtener_estado", (1,), ESTADO),
            ("/vehiculos/1/historial", "obtener_historial", (1,), [HISTORIAL]),
            ("/vehiculos/1/demos", "obtener_demos", (1,), [DEMO]),
            ("/vehiculos/1/demos/3/movimientos", "obtener_movimientos_demo", (1, 3), [DEMO_MOVIMIENTO]),
        ]
        for path, name, args, response in cases:
            with self.subTest(path=path):
                getattr(self.service, name).return_value = response
                status_code, _ = self.call("GET", path)
                self.assertEqual(status_code, 200)
                getattr(self.service, name).assert_called_once_with(*args)

    def test_state_without_previous_row_is_valid_null(self):
        self.service.obtener_estado.return_value = None
        self.assertEqual(self.call("GET", "/vehiculos/1/estado"), (200, None))

    def test_existing_vehicle_may_have_empty_collections(self):
        for suffix, method in [
            ("comandos", "obtener_comandos"), ("historial", "obtener_historial"),
            ("demos", "obtener_demos"), ("demos/3/movimientos", "obtener_movimientos_demo"),
        ]:
            with self.subTest(suffix=suffix):
                getattr(self.service, method).return_value = []
                self.assertEqual(self.call("GET", f"/vehiculos/1/{suffix}"), (200, []))

    def test_join_response_shapes_match_real_selects(self):
        for path, method, fixture, absent in [
            ("estado", "obtener_estado", ESTADO, "id_estado"),
            ("historial", "obtener_historial", [HISTORIAL], "id_comando"),
            ("demos/3/movimientos", "obtener_movimientos_demo", [DEMO_MOVIMIENTO], "id_demo"),
        ]:
            with self.subTest(path=path):
                getattr(self.service, method).return_value = fixture
                status_code, payload = self.call("GET", f"/vehiculos/1/{path}")
                self.assertEqual(status_code, 200)
                actual = payload[0] if isinstance(payload, list) else payload
                expected = fixture[0] if isinstance(fixture, list) else fixture
                self.assertEqual(set(actual), set(expected))
                self.assertNotIn(absent, actual)

    def test_all_thirteen_writes_delegate_with_validated_body(self):
        vehicle_body = {"nombre": "Vehículo de prueba"}
        command_body = {"nombre": "Avanzar", "codigo": -1}
        demo_body = {"nombre": "Secuencia"}
        movement_body = {"id_comando": 2, "orden": 1, "duracion_ms": 1000}
        cases = [
            ("/vehiculos", "crear_vehiculo", (), vehicle_body, VEHICULO, 201),
            ("/vehiculos/1/actualizar", "actualizar_vehiculo", (1,), {**vehicle_body, "activo": True}, VEHICULO, 200),
            ("/vehiculos/1/eliminar", "eliminar_vehiculo", (1,), None, {"eliminado": True, "id": 1}, 200),
            ("/vehiculos/1/comandos", "crear_comando", (1,), command_body, COMANDO, 201),
            ("/vehiculos/1/comandos/2/actualizar", "actualizar_comando", (1, 2), {**command_body, "activo": True}, COMANDO, 200),
            ("/vehiculos/1/comandos/2/eliminar", "eliminar_comando", (1, 2), None, {"eliminado": True, "id": 2}, 200),
            ("/vehiculos/1/demos", "crear_demo", (1,), demo_body, DEMO, 201),
            ("/vehiculos/1/demos/3/actualizar", "actualizar_demo", (1, 3), {**demo_body, "activo": True}, DEMO, 200),
            ("/vehiculos/1/demos/3/eliminar", "eliminar_demo", (1, 3), None, {"eliminado": True, "id": 3}, 200),
            ("/vehiculos/1/demos/3/movimientos", "crear_movimiento_demo", (1, 3), movement_body, DEMO_MOVIMIENTO, 201),
            ("/vehiculos/1/demos/3/movimientos/5/actualizar", "actualizar_movimiento_demo", (1, 3, 5), movement_body, DEMO_MOVIMIENTO, 200),
            ("/vehiculos/1/demos/3/movimientos/5/eliminar", "eliminar_movimiento_demo", (1, 3, 5), None, {"eliminado": True, "id": 5}, 200),
            ("/vehiculos/1/movimientos", "ejecutar_movimiento", (1,), {"id_comando": 2, "origen": "HTTP"}, MOVIMIENTO, 200),
        ]
        for path, name, ids, body, response, expected_code in cases:
            with self.subTest(path=path):
                getattr(self.service, name).return_value = response
                actual_code, _ = self.call("POST", path, body)
                self.assertEqual(actual_code, expected_code)
                call_args = getattr(self.service, name).call_args.args
                self.assertEqual(call_args[:len(ids)], ids)
                if body is not None:
                    self.assertEqual(call_args[-1].model_dump(exclude_unset=True), body)

    def test_route_ids_are_positive_signed_mysql_integers(self):
        for bad in ["0", "-1", "2147483648", "abc"]:
            for path in [f"/vehiculos/{bad}", f"/vehiculos/1/demos/{bad}/movimientos"]:
                with self.subTest(path=path):
                    self.assertEqual(self.call("GET", path)[0], 422)
        self.service.obtener_vehiculo.assert_not_called()
        self.service.obtener_movimientos_demo.assert_not_called()

    def test_demo_movement_rejects_invalid_sp_parameters(self):
        valid = {"id_comando": 2, "orden": 1, "duracion_ms": 1, "pausa_ms": 0}
        for field, bad in [
            ("id_comando", 0), ("id_comando", True), ("orden", 0),
            ("duracion_ms", 0), ("duracion_ms", -1), ("duracion_ms", 2_147_483_648),
            ("pausa_ms", -1), ("pausa_ms", 2_147_483_648), ("pausa_ms", "1"),
        ]:
            with self.subTest(field=field, value=bad):
                self.assertEqual(self.call("POST", "/vehiculos/1/demos/3/movimientos", {**valid, field: bad})[0], 422)
        self.service.crear_movimiento_demo.assert_not_called()

    def test_names_lengths_extra_fields_and_strict_active_are_validated(self):
        cases = [
            ("/vehiculos", {"nombre": "   "}),
            ("/vehiculos", {"nombre": "x" * 81}),
            ("/vehiculos", {"nombre": "Auto", "microcontrolador": "x" * 51}),
            ("/vehiculos", {"nombre": "Auto", "descripcion": "x" * 256}),
            ("/vehiculos", {"nombre": "Auto", "id_vehiculo": 7}),
            ("/vehiculos", {"nombre": "Auto", "activo": True}),
            ("/vehiculos/1/actualizar", {"nombre": "Auto", "activo": 1}),
            ("/vehiculos/1/comandos", {"nombre": "x" * 61, "codigo": 0}),
            ("/vehiculos/1/comandos", {"nombre": "Uno", "codigo": 2_147_483_648}),
            ("/vehiculos/1/comandos", {"nombre": "Uno", "codigo": True}),
            ("/vehiculos/1/movimientos", {"id_comando": 2}),
            ("/vehiculos/1/movimientos", {"id_comando": 2, "origen": " "}),
            ("/vehiculos/1/movimientos", {"id_comando": 2, "origen": "x" * 31}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.call("POST", path, body)[0], 422)

    def test_full_signed_codigo_range_and_parameter_boundaries_are_accepted(self):
        for codigo in [-2_147_483_648, -1, 0, 2_147_483_647]:
            self.assertEqual(ComandoCrear(nombre="Control", codigo=codigo).codigo, codigo)
        data = DemoMovimientoCrear(id_comando=2_147_483_647, orden=2_147_483_647,
                                  duracion_ms=2_147_483_647, pausa_ms=2_147_483_647)
        self.assertEqual(data.duracion_ms, 2_147_483_647)
        self.assertEqual(VehiculoCrear(nombre="  Auto  ").nombre, "Auto")

    def test_swagger_documents_every_write_permission_and_all_sp(self):
        paths = self.app.openapi()["paths"]
        reads = [op for methods in paths.values() for method, op in methods.items() if method == "get"]
        writes = [op for methods in paths.values() for method, op in methods.items() if method == "post"]
        self.assertEqual(len(reads), 7)
        self.assertEqual(len(writes), 13)
        catalog = json.loads((Path(__file__).resolve().parents[1] / "docs" / "contrato_rds.json").read_text(encoding="utf-8"))
        expected_sp = {procedure["name"] for procedure in catalog["procedures"]}
        documented_sp = set(re.findall(r"sp_[a-z_]+", " ".join(op.get("description", "") for op in reads + writes)))
        self.assertEqual(len(expected_sp), 21)
        self.assertEqual(documented_sp, expected_sp)
        for op in writes:
            self.assertTrue(op.get("security"))
            self.assertTrue({"401", "403", "409", "422", "503"}.issubset(op["responses"]))
        for route in router.routes:
            if "POST" in route.methods:
                expected = require_operator if route.path == "/vehiculos/{id_vehiculo}/movimientos" else require_admin
                self.assertIn(expected, [dep.call for dep in route.dependant.dependencies])

    def test_sync_routes_execute_service_outside_event_loop_thread(self):
        event_loop_thread = threading.get_ident()
        service_threads = []
        self.service.listar_vehiculos.side_effect = lambda: (service_threads.append(threading.get_ident()) or [])
        self.assertEqual(self.call("GET", "/vehiculos"), (200, []))
        self.assertNotEqual(service_threads, [event_loop_thread])
        self.assertTrue(all(not inspect.iscoroutinefunction(route.endpoint) for route in router.routes))


if __name__ == "__main__":
    unittest.main()
