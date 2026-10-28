"""Reglas de dominio y polimorfismo de la ruta real, con MySQL sustituido."""

import asyncio
from dataclasses import FrozenInstanceError
import inspect
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from app.domain import Comando, Demo, Vehiculo
from app.errors import ApplicationError
from app.routes.demos import DemoController
from app.routes.health import HealthController
from app.routes.vehiculos import VehiculoController, get_service
from app.routes.websocket import WebSocketController
from app.security import require_operator
from app.services.movimientos import Movimiento, MovimientoDeDemo, MovimientoIndividual
from scripts.demostrar_poo import demostrar
from tests.test_http import request
from tests.test_services import harness


class DomainRulesTests(unittest.TestCase):
    def test_inactive_resources_reject_execution(self):
        cases = [(Vehiculo(1, "Vehículo", False), "exigir_activo"),
                 (Comando(2, 1, "Avanzar", 7, False), "exigir_activo"),
                 (Demo(3, 1, "Secuencia", False), "exigir_activa")]
        for resource, method in cases:
            with self.subTest(resource=type(resource).__name__):
                with self.assertRaises(ApplicationError) as result:
                    getattr(resource, method)()
                self.assertEqual(result.exception.status_code, 409)

    def test_foreign_commands_and_demos_are_not_visible_as_owned_resources(self):
        vehicle = Vehiculo(1, "Vehículo", True)
        for resource in (Comando(2, 9, "Avanzar", 7, True), Demo(3, 9, "Secuencia", True)):
            with self.subTest(resource=type(resource).__name__):
                with self.assertRaises(ApplicationError) as result:
                    resource.exigir_pertenencia(vehicle)
                self.assertEqual(result.exception.status_code, 404)

    def test_domain_state_cannot_be_changed_behind_validation(self):
        command = Comando(2, 1, "Avanzar", 7, False)
        with self.assertRaises(FrozenInstanceError):
            command.activo = True
        with self.assertRaises(ApplicationError):
            command.exigir_activo()


class PolymorphicMovementTests(unittest.TestCase):
    def test_abstract_movement_requires_a_history_policy(self):
        with self.assertRaises(TypeError):
            Movimiento(Vehiculo(1, "Vehículo", True), Comando(11, 1, "Avanzar", 11, True))

    def test_same_execute_call_applies_each_history_policy_and_commits_once(self):
        for movement_type, expected_writes, history_increment in (
            (MovimientoIndividual, ["actualizar_estado", "registrar_historial"], 1),
            (MovimientoDeDemo, ["actualizar_estado"], 0),
        ):
            with self.subTest(movement=movement_type.__name__):
                with harness() as (service, repository, connection, durable):
                    before = len(durable["history"])
                    vehicle = Vehiculo.desde_fila(durable["vehicles"][1])
                    command = Comando.desde_fila(durable["commands"][11])
                    arguments = (vehicle, command, "POO") if movement_type is MovimientoIndividual else (vehicle, command)
                    movement: Movimiento = movement_type(*arguments)
                    with service.manager.transaction() as transaction:
                        result = movement.ejecutar(repository, transaction)
                    self.assertEqual([write[0] for write in repository.writes], expected_writes)
                    self.assertTrue(all(write[1] is connection for write in repository.writes))
                    self.assertEqual(len(durable["history"]), before + history_increment)
                    self.assertEqual(connection.commits, 1)
                    self.assertEqual(connection.rollbacks, 0)
                    self.assertTrue(result.conectado)
                    self.assertEqual(result.estado["id_comando"], 11)
                    self.assertEqual(result.historial is not None, bool(history_increment))

    def test_demo_step_never_reads_or_inserts_history(self):
        with harness() as (service, repository, connection, durable):
            movement = MovimientoDeDemo(Vehiculo.desde_fila(durable["vehicles"][1]),
                                        Comando.desde_fila(durable["commands"][11]))
            with patch.object(repository, "registrar_historial", side_effect=AssertionError("No insertar")), \
                 patch.object(repository, "obtener_historial", side_effect=AssertionError("No consultar")):
                with service.manager.transaction() as transaction:
                    result = movement.ejecutar(repository, transaction)
            self.assertIsNone(result.historial)
            self.assertEqual(connection.commits, 1)

    def test_foreign_command_cannot_write_even_when_called_without_http(self):
        with harness() as (service, repository, connection, durable):
            movement = MovimientoIndividual(Vehiculo.desde_fila(durable["vehicles"][1]),
                                            Comando.desde_fila(durable["commands"][21]), "POO")
            with self.assertRaises(ApplicationError) as result:
                with service.manager.transaction() as transaction:
                    movement.ejecutar(repository, transaction)
            self.assertEqual(result.exception.status_code, 404)
            self.assertEqual(repository.writes, [])
            self.assertEqual(connection.commits, 0)
            self.assertEqual(connection.rollbacks, 1)


class ControllerIntegrationTests(unittest.TestCase):
    def test_all_http_and_websocket_handlers_are_bound_to_controller_instances(self):
        for controller_type, count in ((VehiculoController, 20), (DemoController, 3),
                                       (HealthController, 2), (WebSocketController, 1)):
            with self.subTest(controller=controller_type.__name__):
                controller = controller_type()
                self.assertEqual(len(controller.router.routes), count)
                for route in controller.router.routes:
                    self.assertTrue(inspect.ismethod(route.endpoint))
                    self.assertIs(route.endpoint.__self__, controller)
                    self.assertNotIn("self", inspect.signature(route.endpoint).parameters)

    def test_http_controller_executes_real_service_and_persists_both_changes(self):
        with harness() as (service, repository, connection, durable):
            application = FastAPI()
            application.include_router(VehiculoController().router)
            application.dependency_overrides[get_service] = lambda: service
            application.dependency_overrides[require_operator] = lambda: None
            status, payload = asyncio.run(request(application, "POST", "/vehiculos/1/movimientos",
                                                  {"id_comando": 11, "origen": "POO_HTTP"}))
            self.assertEqual(status, 200)
            self.assertEqual(payload["estado"]["id_comando"], 11)
            self.assertEqual(payload["historial"]["origen"], "POO_HTTP")
            self.assertEqual(connection.commits, 1)
            self.assertEqual([write[0] for write in repository.writes],
                             ["actualizar_estado", "registrar_historial"])
            self.assertEqual(durable["states"][1]["id_comando"], 11)

    def test_local_demonstration_uses_both_policies_without_connecting_to_mysql(self):
        with patch("mysql.connector.connect", side_effect=AssertionError("No abrir MySQL")):
            result = demostrar()
        self.assertEqual(result["modo"], "local_sin_rds")
        self.assertEqual([row["registro_historial"] for row in result["ejecuciones"]], [True, False])
        self.assertEqual(result["historial_total"], 1)


if __name__ == "__main__":
    unittest.main()
