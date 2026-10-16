"""Casos de uso con transacciones reales del manager y datos aislados en memoria.

El conector se sustituye antes de abrir conexiones. Estas pruebas no cargan
credenciales locales, no usan red y nunca escriben en RDS.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import SecretStr

from app.database.connection import DatabaseUnavailableError, MySQLConnectionManager
from app.errors import ApplicationError
from app.models.vehiculos import (
    ComandoActualizar, ComandoCrear, DemoActualizar, DemoCrear,
    DemoMovimientoActualizar, DemoMovimientoCrear, MovimientoEjecutar,
)
from app.services.vehiculos import VehiculoService


NOW = datetime(2026, 10, 15, 12, 0, 0)


def initial_data():
    def vehicle(identifier):
        return {"id_vehiculo": identifier, "nombre": f"Vehículo {identifier}",
                "modelo": None, "descripcion": None, "microcontrolador": None,
                "fecha_registro": NOW, "activo": True}

    def command(identifier, owner):
        return {"id_comando": identifier, "id_vehiculo": owner,
                "nombre": f"Comando {identifier}", "codigo": identifier,
                "descripcion": None, "activo": True}

    def demo(identifier, owner):
        return {"id_demo": identifier, "id_vehiculo": owner,
                "nombre": f"DEMO {identifier}", "descripcion": None, "activo": True}

    return {
        "vehicles": {1: vehicle(1), 2: vehicle(2)},
        "commands": {11: command(11, 1), 21: command(21, 2)},
        "demos": {31: demo(31, 1), 41: demo(41, 2)},
        "movements": {
            51: {"id_demo_movimiento": 51, "id_demo": 31, "id_comando": 11,
                 "orden": 1, "duracion_ms": 500, "pausa_ms": 0},
            61: {"id_demo_movimiento": 61, "id_demo": 41, "id_comando": 21,
                 "orden": 1, "duracion_ms": 500, "pausa_ms": 0},
        },
        "states": {1: {"id_vehiculo": 1, "id_comando": 11,
                       "conectado": True, "fecha_actualizacion": NOW}},
        # Este registro futuro debe seguir primero incluso tras insertar uno.
        "history": [{"id_historial": 999, "id_vehiculo": 1, "comando": "Futuro",
                     "codigo": 11, "fecha_hora": datetime(2035, 1, 1), "origen": "DEMO"}],
    }


class TransactionConnection:
    """Separa estado confirmado y provisional para probar commit y rollback."""

    def __init__(self, durable):
        self.durable = durable
        self.tables = deepcopy(durable)
        self.before = None
        self.starts = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def start_transaction(self, *, readonly=False):
        self.starts.append(readonly)
        self.before = deepcopy(self.durable)
        self.tables = deepcopy(self.durable)

    def commit(self):
        self.commits += 1
        self.durable.clear()
        self.durable.update(deepcopy(self.tables))

    def rollback(self):
        self.rollbacks += 1
        self.tables = deepcopy(self.before)

    def close(self):
        self.closed = True


class MemoryRepository:
    """Representa sólo los resultados del contrato y registra cada escritura."""

    def __init__(self, *, fail_history=False):
        self.fail_history = fail_history
        self.writes = []
        self.history_rows_read = []

    def obtener_vehiculo(self, connection, identifier):
        return connection.tables["vehicles"].get(identifier)

    def obtener_comandos(self, connection, owner):
        return [row for row in connection.tables["commands"].values() if row["id_vehiculo"] == owner]

    def obtener_demos(self, connection, owner):
        return [row for row in connection.tables["demos"].values() if row["id_vehiculo"] == owner]

    def obtener_movimientos_demo(self, connection, identifier):
        rows = []
        for row in connection.tables["movements"].values():
            if row["id_demo"] == identifier:
                command = connection.tables["commands"][row["id_comando"]]
                rows.append({key: value for key, value in row.items() if key != "id_demo"}
                            | {"comando": command["nombre"], "codigo": command["codigo"]})
        return sorted(rows, key=lambda row: row["orden"])

    def obtener_estado(self, connection, owner):
        row = connection.tables["states"].get(owner)
        if row is None:
            return None
        command = connection.tables["commands"][row["id_comando"]]
        return dict(row) | {"vehiculo": connection.tables["vehicles"][owner]["nombre"],
                            "comando": command["nombre"], "codigo": command["codigo"]}

    def obtener_historial(self, connection, owner):
        rows = sorted((r for r in connection.tables["history"] if r["id_vehiculo"] == owner),
                      key=lambda row: (row["fecha_hora"], row["id_historial"]), reverse=True)
        self.history_rows_read.append(deepcopy(rows))
        return rows

    def bloquear_vehiculo(self, connection, identifier):
        return self.obtener_vehiculo(connection, identifier)

    def bloquear_comando(self, connection, identifier):
        return connection.tables["commands"].get(identifier)

    def bloquear_estado(self, connection, owner):
        return connection.tables["states"].get(owner)

    def actualizar_estado(self, connection, owner, command, connected):
        self.writes.append(("actualizar_estado", connection, owner, command, connected))
        connection.tables["states"][owner] = {
            "id_vehiculo": owner, "id_comando": command,
            "conectado": connected, "fecha_actualizacion": NOW,
        }

    def registrar_historial(self, connection, owner, command, origin):
        self.writes.append(("registrar_historial", connection, owner, command, origin))
        row = connection.tables["commands"][command]
        connection.tables["history"].append({
            "id_historial": 100, "id_vehiculo": owner, "comando": row["nombre"],
            "codigo": row["codigo"], "fecha_hora": NOW, "origen": origin,
        })
        if self.fail_history:
            # También se restaura una inserción provisional del segundo SP.
            raise DatabaseUnavailableError("query")
        return 100

    def crear_comando(self, connection, owner, name, code, description):
        self.writes.append(("crear_comando", connection))
        connection.tables["commands"][22] = {
            "id_comando": 22, "id_vehiculo": owner, "nombre": name,
            "codigo": code, "descripcion": description, "activo": True,
        }
        return 22

    def actualizar_comando(self, connection, identifier, name, code, description, active):
        self.writes.append(("actualizar_comando", connection))
        connection.tables["commands"][identifier].update(
            nombre=name, codigo=code, descripcion=description, activo=active)

    def eliminar_comando(self, connection, identifier):
        self.writes.append(("eliminar_comando", connection))
        del connection.tables["commands"][identifier]

    def crear_demo(self, connection, owner, name, description):
        self.writes.append(("crear_demo", connection))
        connection.tables["demos"][42] = {"id_demo": 42, "id_vehiculo": owner,
                                           "nombre": name, "descripcion": description, "activo": True}
        return 42

    def actualizar_demo(self, connection, identifier, name, description, active):
        self.writes.append(("actualizar_demo", connection))
        connection.tables["demos"][identifier].update(nombre=name, descripcion=description, activo=active)

    def eliminar_demo(self, connection, identifier):
        self.writes.append(("eliminar_demo", connection))
        del connection.tables["demos"][identifier]

    def crear_movimiento_demo(self, connection, demo, command, order, duration, pause):
        self.writes.append(("crear_movimiento_demo", connection))
        connection.tables["movements"][62] = {
            "id_demo_movimiento": 62, "id_demo": demo, "id_comando": command,
            "orden": order, "duracion_ms": duration, "pausa_ms": pause,
        }

    def actualizar_movimiento_demo(self, connection, identifier, command, order, duration, pause):
        self.writes.append(("actualizar_movimiento_demo", connection))
        connection.tables["movements"][identifier].update(
            id_comando=command, orden=order, duracion_ms=duration, pausa_ms=pause)

    def eliminar_movimiento_demo(self, connection, identifier):
        self.writes.append(("eliminar_movimiento_demo", connection))
        del connection.tables["movements"][identifier]


@contextmanager
def harness(data=None, *, fail_history=False):
    durable = deepcopy(data if data is not None else initial_data())
    connection = TransactionConnection(durable)
    settings = SimpleNamespace(mysql_host="example.invalid", mysql_port=3306,
                               mysql_user="example_user", mysql_password=SecretStr("test_value"),
                               mysql_database="isolated")
    manager = MySQLConnectionManager(settings)
    repository = MemoryRepository(fail_history=fail_history)
    with patch("app.database.connection.mysql.connector.connect", return_value=connection) as connect:
        yield VehiculoService(manager, repository), repository, connection, durable
        # Garantiza que todas las operaciones sí pasaron por el manager real.
        if connect.call_count:
            assert connection.closed


class MovementTransactionTests(unittest.TestCase):
    def test_second_procedure_failure_rolls_back_both_changes_on_same_connection(self):
        before = initial_data()
        before["commands"][12] = dict(before["commands"][11]) | {
            "id_comando": 12, "nombre": "Comando 12", "codigo": 12,
        }
        with harness(before, fail_history=True) as (service, repository, connection, durable):
            with self.assertRaises(DatabaseUnavailableError) as result:
                service.ejecutar_movimiento(1, MovimientoEjecutar(id_comando=12, origen="HTTP"))
            self.assertEqual(result.exception.code, "query")
            self.assertEqual([write[0] for write in repository.writes],
                             ["actualizar_estado", "registrar_historial"])
            self.assertTrue(all(write[1] is connection for write in repository.writes))
            self.assertEqual(connection.starts, [False])
            self.assertEqual(connection.commits, 0)
            self.assertEqual(connection.rollbacks, 1)
            self.assertTrue(connection.closed)
            self.assertEqual(connection.tables, before)
            self.assertEqual(durable, before)

    def test_commit_preserves_connected_true_false_and_missing_state_false(self):
        for previous in (True, False, None):
            with self.subTest(previous=previous):
                data = initial_data()
                if previous is None:
                    data["states"].clear()
                else:
                    data["states"][1]["conectado"] = previous
                with harness(data) as (service, repository, connection, durable):
                    result = service.ejecutar_movimiento(1, MovimientoEjecutar(id_comando=11, origen="HTTP"))
                    self.assertEqual(result.conectado, previous if previous is not None else False)
                    self.assertEqual(result.estado.conectado, result.conectado)
                    self.assertEqual(durable["states"][1]["conectado"], result.conectado)
                    self.assertEqual(result.historial.id_historial, 100)
                    self.assertEqual(result.historial.origen, "HTTP")
                    # No confunde el registro insertado con el primero del SP.
                    self.assertEqual(repository.history_rows_read[0][0]["id_historial"], 999)
                    self.assertEqual(len(durable["history"]), 2)
                    self.assertEqual(connection.commits, 1)
                    self.assertEqual(connection.rollbacks, 0)
                    self.assertTrue(connection.closed)
                    self.assertTrue(all(write[1] is connection for write in repository.writes))

    def test_vehicle_and_command_rejections_happen_before_any_write(self):
        cases = [
            ("missing_vehicle", 404, "vehicle_not_found", 11),
            ("inactive_vehicle", 409, "inactive_vehicle", 11),
            ("missing_command", 404, "command_not_found", 999),
            ("foreign_command", 404, "command_not_found", 21),
            ("inactive_command", 409, "inactive_command", 11),
        ]
        for case, status, code, command in cases:
            with self.subTest(case=case):
                data = initial_data()
                if case == "missing_vehicle":
                    del data["vehicles"][1]
                elif case == "inactive_vehicle":
                    data["vehicles"][1]["activo"] = False
                elif case == "inactive_command":
                    data["commands"][11]["activo"] = False
                with harness(data) as (service, repository, connection, durable):
                    with self.assertRaises(ApplicationError) as result:
                        service.ejecutar_movimiento(1, MovimientoEjecutar(id_comando=command, origen="HTTP"))
                    self.assertEqual(result.exception.status_code, status)
                    self.assertEqual(result.exception.code, code)
                    self.assertEqual(repository.writes, [])
                    self.assertEqual(durable, data)
                    self.assertEqual(connection.commits, 0)
                    self.assertEqual(connection.rollbacks, 1)


class HistoryTests(unittest.TestCase):
    def test_limits_representation_to_ten_preserves_sp_order_and_all_stored_rows(self):
        for count in (0, 3, 10, 14):
            with self.subTest(count=count):
                data = initial_data()
                data["history"] = [
                    {"id_historial": identifier, "id_vehiculo": 1, "comando": "Test",
                     "codigo": 11, "fecha_hora": NOW - timedelta(minutes=index), "origen": "HTTP"}
                    for index, identifier in enumerate(range(80, 80 + count))
                ]
                before = deepcopy(data)
                with harness(data) as (service, repository, connection, durable):
                    result = service.obtener_historial(1)
                    self.assertEqual([row.id_historial for row in result], list(range(80, 80 + min(10, count))))
                    self.assertEqual(len(repository.history_rows_read[0]), count)
                    self.assertEqual(repository.writes, [])
                    self.assertEqual(durable, before)
                    self.assertEqual(connection.starts, [True])

    def test_missing_vehicle_is_404_but_existing_empty_history_is_valid(self):
        with harness() as (service, repository, connection, durable):
            self.assertEqual(service.obtener_historial(2), [])
        with harness() as (service, repository, connection, durable):
            with self.assertRaises(ApplicationError) as result:
                service.obtener_historial(999)
            self.assertEqual(result.exception.status_code, 404)
            self.assertEqual(result.exception.code, "vehicle_not_found")
            self.assertEqual(repository.history_rows_read, [])


class ResourceOwnershipTests(unittest.TestCase):
    def assert_rejected_without_write(self, operation, code):
        with harness() as (service, repository, connection, durable):
            before = deepcopy(durable)
            with self.assertRaises(ApplicationError) as result:
                operation(service)
            self.assertEqual(result.exception.status_code, 404)
            self.assertEqual(result.exception.code, code)
            self.assertEqual(repository.writes, [])
            self.assertEqual(connection.commits, 0)
            self.assertEqual(connection.rollbacks, 1)
            self.assertEqual(durable, before)

    def test_foreign_commands_cannot_be_updated_or_deleted(self):
        operations = [
            lambda s: s.actualizar_comando(1, 21, ComandoActualizar(nombre="Cambio", codigo=7, activo=True)),
            lambda s: s.eliminar_comando(1, 21),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                self.assert_rejected_without_write(operation, "command_not_found")

    def test_foreign_demos_cannot_be_queried_or_managed_through_other_vehicle(self):
        operations = [
            lambda s: s.obtener_movimientos_demo(1, 41),
            lambda s: s.actualizar_demo(1, 41, DemoActualizar(nombre="Cambio", activo=True)),
            lambda s: s.eliminar_demo(1, 41),
            lambda s: s.crear_movimiento_demo(1, 41, DemoMovimientoCrear(id_comando=11, orden=2, duracion_ms=1)),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                self.assert_rejected_without_write(operation, "demo_not_found")

    def test_foreign_movements_cannot_be_updated_or_deleted_from_other_demo(self):
        operations = [
            lambda s: s.actualizar_movimiento_demo(
                1, 31, 61, DemoMovimientoActualizar(id_comando=11, orden=2, duracion_ms=100)),
            lambda s: s.eliminar_movimiento_demo(1, 31, 61),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                self.assert_rejected_without_write(operation, "demo_movement_not_found")

    def test_demo_movement_rejects_command_of_another_vehicle_on_create_and_update(self):
        operations = [
            lambda s: s.crear_movimiento_demo(
                1, 31, DemoMovimientoCrear(id_comando=21, orden=2, duracion_ms=100)),
            lambda s: s.actualizar_movimiento_demo(
                1, 31, 51, DemoMovimientoActualizar(id_comando=21, orden=2, duracion_ms=100)),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                self.assert_rejected_without_write(operation, "command_not_found")

    def test_catalog_command_creation_does_not_execute_movement(self):
        with harness() as (service, repository, connection, durable):
            result = service.crear_comando(1, ComandoCrear(nombre="Girar", codigo=7))
            self.assertEqual(result.id_comando, 22)
            self.assertEqual(result.id_vehiculo, 1)
            self.assertEqual([write[0] for write in repository.writes], ["crear_comando"])
            self.assertEqual(connection.commits, 1)
            self.assertEqual(len(durable["history"]), 1)

    def test_owned_command_and_demo_updates_and_deletes_are_committed(self):
        operations = [
            (lambda s: s.actualizar_comando(1, 11, ComandoActualizar(nombre="Nuevo", codigo=9, activo=False)),
             lambda r, d: (r.nombre == "Nuevo" and not d["commands"][11]["activo"])),
            (lambda s: s.eliminar_comando(1, 11), lambda r, d: (r.eliminado and 11 not in d["commands"])),
            (lambda s: s.crear_demo(1, DemoCrear(nombre="Ruta")), lambda r, d: r.id_demo == 42),
            (lambda s: s.actualizar_demo(1, 31, DemoActualizar(nombre="Ruta nueva", activo=False)),
             lambda r, d: r.nombre == "Ruta nueva" and not d["demos"][31]["activo"]),
            (lambda s: s.eliminar_demo(1, 31), lambda r, d: r.eliminado and 31 not in d["demos"]),
        ]
        for operation, verify in operations:
            with self.subTest(operation=operation):
                with harness() as (service, repository, connection, durable):
                    result = operation(service)
                    self.assertTrue(verify(result, durable))
                    self.assertEqual(connection.commits, 1)
                    self.assertEqual(connection.rollbacks, 0)

    def test_owned_demo_movement_create_update_delete_return_confirmed_values(self):
        with harness() as (service, repository, connection, durable):
            result = service.crear_movimiento_demo(
                1, 31, DemoMovimientoCrear(id_comando=11, orden=2, duracion_ms=750, pausa_ms=40))
            self.assertEqual(result.id_demo_movimiento, 62)
            self.assertEqual(result.orden, 2)
            self.assertEqual(result.comando, "Comando 11")
            self.assertEqual(durable["movements"][62]["duracion_ms"], 750)
            self.assertEqual(connection.commits, 1)
        with harness() as (service, repository, connection, durable):
            result = service.actualizar_movimiento_demo(
                1, 31, 51, DemoMovimientoActualizar(id_comando=11, orden=3, duracion_ms=250, pausa_ms=50))
            self.assertEqual((result.orden, result.duracion_ms, result.pausa_ms), (3, 250, 50))
            self.assertEqual(connection.commits, 1)
        with harness() as (service, repository, connection, durable):
            result = service.eliminar_movimiento_demo(1, 31, 51)
            self.assertTrue(result.eliminado)
            self.assertNotIn(51, durable["movements"])
            self.assertEqual(connection.commits, 1)


if __name__ == "__main__":
    unittest.main()
