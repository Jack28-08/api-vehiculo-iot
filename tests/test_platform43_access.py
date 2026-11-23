"""Prueba ASGI del alcance 4.3 y ejercicio opcional de la copia MySQL local."""

import asyncio
import json
import os
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from app.main import app
from app.models.vehiculos import ComandoCrear, VehiculoCrear
from app.platform43 import auth, commercial, commercial_routes
from app.routes.vehiculos import get_service
from app.runtime import CoordinatedVehiculoService, runtime
from app.services.demo_data import DemoDataService
from tests.test_platform43_commercial import LabManager, ORIGIN, call_http


class SessionUnitTests(TestCase):
    def test_password_uses_independent_salts_and_handles_unicode(self):
        candidate = "Hidráulica de prueba !"
        first, second = auth.hash_password(candidate), auth.hash_password(candidate)
        self.assertNotEqual(first, second)
        self.assertTrue(auth.password_matches(first, candidate))
        self.assertFalse(auth.password_matches(first, candidate + "x"))
        self.assertFalse(auth.password_matches("formato-invalido", candidate))


class GateTests(IsolatedAsyncioTestCase):
    async def test_managed_mode_closes_old_public_vehicle_and_evaluation_access(self):
        with patch.object(auth, "platform_enabled", return_value=True):
            for path in ("/vehiculos", "/vehiculos/1", "/vehiculos/1/estado", "/vehiculos/1/historial"):
                status, _ = await call_http("GET", path)
                self.assertEqual(status, 401)
            status, _ = await call_http("GET", "/evaluacion/vehiculos/1")
            self.assertEqual(status, 404)

    async def test_monitor_requires_ticket_with_feature_enabled(self):
        with patch.object(auth, "platform_enabled", return_value=True):
            task, incoming, outgoing = await monitor(1)
            self.assertEqual(await outgoing.get(), {"type": "websocket.close", "code": 4403, "reason": ""})
            await task


async def monitor(vehicle_id, ticket=None):
    path = f"/ws/vehiculos/{vehicle_id}"
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    scope = {"type":"websocket", "asgi":{"version":"3.0"}, "http_version":"1.1",
             "scheme":"ws", "path":path, "raw_path":path.encode(), "root_path":"",
             "query_string":b"", "headers":[(b"origin", ORIGIN)], "client":("127.0.0.1", 42000),
             "server":("127.0.0.1", 8000), "subprotocols":["node01", "ticket." + ticket] if ticket else []}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    return task, incoming, outgoing


@skipUnless(os.getenv("IOT43_FULL_LAB_PORT"), "requiere copia completa vehiculo_iot_lab en MySQL efímero")
class EndToEnd43Tests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        port = int(os.environ["IOT43_FULL_LAB_PORT"])
        if port in {3306, 33060} or not 1024 <= port <= 65535:
            raise ValueError("El puerto debe pertenecer al MySQL efímero.")
        manager = LabManager(port, "vehiculo_iot_lab")
        with manager.transaction(readonly=True) as connection, manager.cursor(connection) as cursor:
            cursor.execute("SELECT COUNT(*) FROM iot43_migraciones")
            self.assertTrue(cursor.fetchone()[0])
        self.manager = manager
        self.suffix = uuid4().hex[:10]
        self.secret = uuid4().hex
        self.data = DemoDataService(manager)
        self.addCleanup(app.dependency_overrides.clear)
        app.dependency_overrides[get_service] = lambda: CoordinatedVehiculoService(runtime, self.data)
        for target, name, value in [
            (auth, "platform_enabled", lambda: True), (auth, "manager", manager),
            (auth, "login_quota", auth.LoginQuota()), (commercial, "store", commercial.CommercialStore(manager)),
            (commercial_routes, "reservation_quota", commercial_routes.ReservationQuota()),
            (runtime, "data", self.data), (runtime.demos, "data", self.data),
        ]:
            context = patch.object(target, name, value)
            context.start()
            self.addCleanup(context.stop)
        runtime.bind_loop()
        self.addAsyncCleanup(runtime.shutdown)
        email = f"admin-{self.suffix}@laboratorio.invalid"
        with manager.transaction() as connection:
            auth.execute(connection, """INSERT INTO iot43_cuentas (correo,nombre,hash_contrasena,rol)
                         VALUES (%s,'Administrador aislado',%s,'administrador')""",
                         (email, auth.hash_password(self.secret)))
        self.admin = (await self.login(email))["token"]

    async def result(self, status, method, url, body=None, token=None):
        actual, payload = await call_http(method, url, body, token)
        self.assertEqual(actual, status, f"{method} {url}")
        return payload

    async def login(self, email):
        return await self.result(200, "POST", "/plataforma/auth/login", {"usuario":email,"contrasena":self.secret})

    async def account(self, marker):
        email = f"{marker}-{self.suffix}@laboratorio.invalid"
        account = await self.result(201,"POST","/plataforma/admin/clientes",
                                    {"nombre":marker,"correo":email,"contrasena":self.secret}, self.admin)
        return account["id"], (await self.login(email))["token"]

    async def unit(self, state):
        vehicle = self.data.crear_vehiculo(VehiculoCrear(nombre=f"Unidad virtual {self.suffix}"))
        command = self.data.crear_comando(vehicle.id_vehiculo, ComandoCrear(nombre="AVANZAR", codigo=2))
        denied = self.data.crear_comando(vehicle.id_vehiculo, ComandoCrear(nombre="GRUA", codigo=4))
        model = await self.result(201,"POST","/plataforma/admin/modelos",{
            "sku":f"LAB-{self.suffix}","nombre":"Modelo local","publicado":True,
            "capacidades":[{"codigo":2,"control":True,"invitado":True}]}, self.admin)
        unit = await self.result(201,"POST","/plataforma/admin/unidades",{
            "id_vehiculo":vehicle.id_vehiculo,"id_modelo":model["id"],"serie":f"LAB-{self.suffix}",
            "nombre":"Unidad local","estado":state,"fisico":False}, self.admin)
        return unit, command.id_comando, denied.id_comando

    async def test_owner_demo_stop_and_ws_revocation(self):
        unit, command, denied = await self.unit("inventario")
        vehicle = unit["id_vehiculo"]
        a_id, a = await self.account("A")
        b_id, b = await self.account("B")
        assignment = f"/plataforma/admin/unidades/{unit['id']}/asignacion"
        assigned = await self.result(200,"POST",assignment,{"id_cliente":a_id,"version":unit["version"]},self.admin)
        prefix = f"/vehiculos/{vehicle}"
        await self.result(403,"GET",prefix+"/historial",token=b)
        await self.result(403,"POST",prefix+"/detener",token=self.admin)
        catalog = await self.result(200,"GET",prefix+"/comandos",token=a)
        self.assertEqual([row["id_comando"] for row in catalog],[command])
        body = {"id_comando":denied,"origen":"LAB"}
        await self.result(403,"POST",prefix+"/movimientos",body,a)
        # El historial histórico fecha las filas con precisión de segundos.
        await asyncio.sleep(1.05)
        await self.result(200,"POST",prefix+"/movimientos",{**body,"id_comando":command},a)
        history = await self.result(200,"GET",prefix+"/historial",token=a)
        self.assertEqual(len(history),1)
        ticket = (await self.result(201,"POST","/plataforma/tickets",{"id_vehiculo":vehicle},a))["ticket"]
        task, incoming, outgoing = await monitor(vehicle, ticket)
        self.addAsyncCleanup(self._close, task, incoming)
        self.assertEqual((await asyncio.wait_for(outgoing.get(),4))["subprotocol"],"node01")
        initial = json.loads((await asyncio.wait_for(outgoing.get(),4))["text"])
        self.assertEqual(initial["id_vehiculo"],vehicle)
        replay, _, rejected = await monitor(vehicle,ticket)
        self.assertEqual((await asyncio.wait_for(rejected.get(),4))["code"],4403)
        await replay
        demo = await self.result(201,"POST","/plataforma/demos",{
            "id_unidad":unit["id"],"nombre":"Secuencia de laboratorio","pasos":[{"codigo":2,"duracion_ms":350}]},a)
        await self.result(202,"POST",f"/plataforma/demos/{demo['id']}/ejecutar",{"id_unidad":unit["id"]},a)
        await self.result(200,"POST",prefix+"/detener",token=a)
        self.assertEqual(len(await self.result(200,"GET",prefix+"/historial",token=a)),1)
        await self.result(200,"POST",assignment,{"id_cliente":b_id,"version":assigned["version"]},self.admin)
        await self.result(403,"GET",prefix+"/historial",token=a)
        self.assertEqual(await self.result(200,"GET",prefix+"/historial",token=b),[])
        self.assertIsNone(await self.result(200,"GET",prefix+"/estado",token=b))
        await self.result(200,"POST",prefix+"/movimientos",{**body,"id_comando":command},b)
        async with asyncio.timeout(5):
            while (message := await outgoing.get())["type"] != "websocket.close":
                if message["type"] == "websocket.send":
                    self.assertNotEqual(json.loads(message["text"])["tipo"], "movimiento_individual")
        self.assertEqual(message["code"],4403)
        await self.result(204,"POST","/plataforma/auth/contrasena",{"actual":self.secret,"nueva":uuid4().hex},b)
        await self.result(401,"GET","/plataforma/perfil",token=b)

    async def test_guest_expires_without_legacy_or_admin_access(self):
        unit, command, denied = await self.unit("exhibicion")
        prefix = f"/vehiculos/{unit['id_vehiculo']}"
        reservation = await self.result(201,"POST","/plataforma/reservas",{"id_unidad":unit["id"]})
        guest = reservation["token"]
        await self.result(409,"POST","/plataforma/reservas",{"id_unidad":unit["id"]})
        await self.result(403,"GET","/plataforma/admin/modelos",token=guest)
        await self.result(403,"POST",prefix+"/movimientos",{"id_comando":denied,"origen":"LAB"},guest)
        await self.result(200,"POST",prefix+"/movimientos",{"id_comando":command,"origen":"LAB"},guest)
        self.assertEqual(await self.result(200,"GET",prefix+"/historial",token=guest),[])
        with self.manager.transaction() as connection:
            auth.execute(connection,"UPDATE iot43_reservas SET expires_at=UTC_TIMESTAMP(6) WHERE id=%s",
                         (reservation["id_reserva"],))
        await self.result(401,"POST",prefix+"/movimientos",{"id_comando":command,"origen":"LAB"},guest)

    @staticmethod
    async def _close(task, incoming):
        if not task.done():
            incoming.put_nowait({"type":"websocket.disconnect","code":1000})
        await asyncio.wait_for(task,5)
