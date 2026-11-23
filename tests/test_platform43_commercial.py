"""Contratos comerciales contra una instancia MySQL local de laboratorio."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import mysql.connector

from app.database.connection import MySQLConnectionManager
from app.main import app
from app.platform43 import auth, commercial, commercial_routes


SECRET_FIXTURE = "Credencial-local-de-prueba-43!"
ORIGIN = b"http://localhost:5500"
MIGRATIONS = Path(__file__).resolve().parents[1] / "scripts" / "migrations"


class LabManager(MySQLConnectionManager):
    def __init__(self, port, schema):
        super().__init__()
        self.port = port
        self.schema = schema

    def _connection_options(self):
        return dict(host="127.0.0.1", port=self.port, user="root", database=self.schema, time_zone="+00:00",
                    autocommit=False, connection_timeout=3)


def consume(cursor, script):
    cursor.execute(script)
    while True:
        if cursor.with_rows:
            cursor.fetchall()
        if not cursor.nextset():
            break


async def call_http(method, url, payload=None, token=None):
    target = urlsplit(url)
    headers = [(b"content-type", b"application/json"), (b"origin", ORIGIN)]
    if token:
        headers.append((b"authorization", ("Bearer " + token).encode()))
    request = dict(type="http", asgi={"version": "3.0"}, http_version="1.1", method=method,
                   scheme="http", path=target.path, raw_path=target.path.encode(),
                   query_string=target.query.encode(), root_path="", headers=headers,
                   client=("127.0.0.1", 42000), server=("127.0.0.1", 8000))
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": json.dumps(payload).encode() if payload is not None else b"",
                    "more_body": False}
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    await app(request, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(body) if body else None


def call(method, path, payload=None, token=None):
    return asyncio.run(call_http(method, "/plataforma" + path, payload, token))


@unittest.skipUnless(os.environ.get("IOT43_TEST_MYSQL_PORT"), "requiere servidor MySQL efímero de loopback")
class CommercialMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        port = int(os.environ["IOT43_TEST_MYSQL_PORT"])
        if not 1024 <= port <= 65535 or port == 3306:
            raise ValueError("Usa un puerto de laboratorio distinto de MySQL predeterminado.")
        cls.schema = "iot43_" + uuid4().hex + "_lab"
        cls.host = mysql.connector.connect(host="127.0.0.1", port=port, user="root", connection_timeout=3)
        cls.addClassCleanup(cls.host.close)
        with cls.host.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{cls.schema}`")
        cls.addClassCleanup(cls.drop_schema)
        cls.manager = LabManager(port, cls.schema)
        with cls.manager.connection() as db, cls.manager.cursor(db) as cursor:
            consume(cursor, """
                CREATE TABLE vehiculos (id_vehiculo INT AUTO_INCREMENT PRIMARY KEY, nombre VARCHAR(80) NOT NULL,
                    descripcion VARCHAR(255), activo BOOLEAN NOT NULL DEFAULT 1) ENGINE=InnoDB;
                CREATE TABLE comandos (id_comando INT AUTO_INCREMENT PRIMARY KEY, id_vehiculo INT NOT NULL,
                    nombre VARCHAR(60) NOT NULL, codigo INT NOT NULL, activo BOOLEAN NOT NULL DEFAULT 1,
                    CONSTRAINT fk_lab_comando_vehiculo FOREIGN KEY (id_vehiculo) REFERENCES vehiculos(id_vehiculo),
                    UNIQUE (id_vehiculo,codigo)) ENGINE=InnoDB;
                CREATE PROCEDURE sp_obtener_vehiculo(IN vehicle INT)
                    SELECT * FROM vehiculos WHERE id_vehiculo=vehicle;
                CREATE PROCEDURE sp_obtener_comandos_vehiculo(IN vehicle INT)
                    SELECT * FROM comandos WHERE id_vehiculo=vehicle;
                INSERT INTO vehiculos (nombre) VALUES ('Banco protegido'),('Exhibición virtual'),('Unidad comprador');
                INSERT INTO comandos (id_vehiculo,nombre,codigo)
                    VALUES (1,'AVANZAR',2),(2,'AVANZAR',2),(2,'SUBIR_GRUA',4),(3,'AVANZAR',2);
            """)
            consume(cursor, (MIGRATIONS / "001_schema.sql").read_text(encoding="utf8"))
            cursor.execute("INSERT INTO iot43_cuentas (correo,nombre,rol,hash_contrasena) VALUES (%s,%s,%s,%s)",
                           ("admin@fixture.invalid", "Admin de laboratorio", "administrador",
                            auth.hash_password(SECRET_FIXTURE)))
            db.commit()
        scopes = [patch.object(auth, "platform_enabled", return_value=True), patch.object(auth, "manager", cls.manager),
                  patch.object(auth, "revoke_vehicle"), patch.object(commercial, "store", commercial.CommercialStore(cls.manager)),
                  patch.object(commercial_routes, "reservation_quota", commercial_routes.ReservationQuota())]
        for scope in scopes:
            scope.start()
            cls.addClassCleanup(scope.stop)
        code, access = call("POST", "/auth/login", {"usuario": "admin@fixture.invalid", "contrasena": SECRET_FIXTURE})
        if code != 200:
            raise AssertionError(f"No se pudo iniciar el admin de laboratorio: {code}")
        cls.admin = access["token"]

    @classmethod
    def drop_schema(cls):
        with cls.host.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{cls.schema}`")

    def result(self, expected, method, path, data=None, token=None):
        status, body = call(method, path, data, token)
        if status != expected:
            self.fail(f"{method} {path}: se esperaba {expected}, devolvió {status}")
        return body

    def model(self, sku):
        return self.result(201, "POST", "/admin/modelos", {
            "sku": sku, "nombre": f"Modelo {sku}", "publicado": True,
            "capacidades": [{"codigo": 2, "control": True, "invitado": True},
                            {"codigo": 4, "control": True, "invitado": False}],
        }, self.admin)

    def unit(self, model, vehicle, series, state, physical=False):
        return self.result(201, "POST", "/admin/unidades", {
            "id_vehiculo": vehicle, "id_modelo": model, "serie": series, "nombre": f"Unidad {series}",
            "estado": state, "fisico": physical,
        }, self.admin)

    def account(self, letter):
        email = f"comprador-{letter}@fixture.invalid"
        customer = self.result(201, "POST", "/admin/clientes", {
            "correo": email, "nombre": f"Comprador {letter}", "contrasena": SECRET_FIXTURE,
        }, self.admin)
        session = self.result(200, "POST", "/auth/login", {"usuario": email, "contrasena": SECRET_FIXTURE})
        self.assertNotIn("hash_contrasena", customer)
        return customer, session["token"]

    def test_01_guest_is_exclusive_revocable_and_virtual(self):
        model = self.model("GUEST")
        physical = self.unit(model["id"], 1, "PHYSICAL", "exhibicion", True)
        guest = self.unit(model["id"], 2, "GUEST", "exhibicion")
        catalog = self.result(200, "GET", "/catalogo")
        units = next(item["unidades"] for item in catalog if item["id"] == model["id"])
        self.assertEqual([unit["id"] for unit in units], [guest["id"]])
        self.assertNotIn("id_vehiculo", units[0])
        self.result(404, "POST", "/reservas", {"id_unidad": physical["id"]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(call, "POST", "/reservas", {"id_unidad": guest["id"]}) for _ in range(2)]
            attempts = [request.result(timeout=8) for request in requests]
        self.assertEqual(sorted(code for code, _ in attempts), [201, 409])
        session = next(body for code, body in attempts if code == 201)
        token = session["token"]
        self.result(403, "GET", "/admin/modelos", token=token)
        self.result(204, "DELETE", "/reservas/actual", token=token)
        self.result(401, "GET", "/perfil", token=token)
        second = self.result(201, "POST", "/reservas", {"id_unidad": guest["id"]})
        self.result(200, "POST", f"/admin/reservas/{second['id_reserva']}/revocar", token=self.admin)
        self.result(401, "GET", "/perfil", token=second["token"])
        expiring = self.result(201, "POST", "/reservas", {"id_unidad": guest["id"]})
        with self.manager.transaction() as db:
            commercial.store.write(db, "UPDATE iot43_reservas SET expires_at=UTC_TIMESTAMP(6) WHERE id=%s",
                                   (expiring["id_reserva"],))
        self.result(401, "GET", "/perfil", token=expiring["token"])
        replacement = self.result(201, "POST", "/reservas", {"id_unidad": guest["id"]})
        self.assertNotEqual(replacement["id_reserva"], expiring["id_reserva"])
        self.result(204, "DELETE", "/reservas/actual", token=replacement["token"])

    def test_02_ownership_demoplay_and_audit(self):
        model = self.model("VENTA")
        unit = self.unit(model["id"], 3, "VENTA", "inventario")
        a, token_a = self.account("A")
        b, token_b = self.account("B")
        path = f"/admin/unidades/{unit['id']}/asignacion"
        assigned = self.result(200, "POST", path, {"id_cliente": a["id"], "version": unit["version"]}, self.admin)
        self.result(409, "POST", path, {"id_cliente": b["id"], "version": unit["version"]}, self.admin)
        self.assertEqual([row["id"] for row in self.result(200, "GET", "/mis-unidades", token=token_a)], [unit["id"]])
        self.assertEqual(self.result(200, "GET", "/mis-unidades", token=token_b), [])
        self.result(403, "GET", "/admin/clientes", token=token_a)
        self.result(404, "GET", f"/demos?id_unidad={unit['id']}", token=token_b)
        demo_data = {"nombre": "Secuencia propia", "id_unidad": unit["id"],
                     "pasos": [{"codigo": 2, "duracion_ms": 180, "pausa_ms": 100}]}
        demo = self.result(201, "POST", "/demos", demo_data, token_a)
        self.assertEqual(demo["total_ms"], 280)
        self.result(409, "POST", "/demos", {**demo_data, "pasos": [{"codigo": 4, "duracion_ms": 180}]}, token_a)
        with self.manager.transaction() as db:
            plan = commercial.get_demo_plan(db, auth.Actor(a["id"], 123, "comprador", "A", None), demo["id"], unit["id"])
        self.assertEqual(plan["pasos"][0]["codigo"], 2)
        self.assertNotEqual(plan["pasos"][0]["id_comando"], 2)
        new = self.result(200, "POST", path, {"id_cliente": b["id"], "version": assigned["version"]}, self.admin)
        self.assertEqual(new["version"], assigned["version"] + 1)
        self.assertEqual(self.result(200, "GET", "/mis-unidades", token=token_a), [])
        self.assertEqual(self.result(200, "GET", f"/demos?id_unidad={unit['id']}", token=token_b), [])
        self.result(404, "PUT", f"/demos/{demo['id']}", demo_data, token_b)
        factory_data = {"nombre": "Secuencia de fábrica", "id_modelo": model["id"],
                        "pasos": [{"codigo": 2, "duracion_ms": 200}, {"codigo": 2, "duracion_ms": 350}]}
        factory = self.result(201, "POST", "/demos", factory_data, self.admin)
        self.result(409, "PUT", f"/demos/{factory['id']}", factory_data, self.admin)
        updated = self.result(200, "PUT", f"/demos/{factory['id']}",
                              {**factory_data, "version": factory["version"], "pasos": factory_data["pasos"][::-1]},
                              self.admin)
        self.assertEqual([(step["orden"], step["duracion_ms"]) for step in updated["pasos"]], [(1, 350), (2, 200)])
        compatible = self.result(200, "GET", f"/demos?id_unidad={unit['id']}", token=token_b)
        self.assertEqual([row["id"] for row in compatible], [factory["id"]])
        self.result(403, "DELETE", f"/demos/{factory['id']}", token=token_b)
        history = self.result(200, "GET", f"/admin/unidades/{unit['id']}/titularidad", token=self.admin)
        self.assertEqual([row["id_cliente"] for row in history], [b["id"], a["id"]])
        self.assertIsNotNone(history[1]["fin"])
        self.result(200, "PATCH", f"/admin/clientes/{b['id']}", {"activo": False}, self.admin)
        self.result(401, "GET", "/perfil", token=token_b)
        audit = self.result(200, "GET", "/admin/auditoria", token=self.admin)
        self.assertIn("rechazado", {row["resultado"] for row in audit})
        self.assertNotIn(SECRET_FIXTURE, json.dumps(audit))
