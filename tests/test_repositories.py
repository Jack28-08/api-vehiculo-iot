"""Pruebas aisladas del protocolo CALL, sin RDS ni credenciales locales."""

import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from mysql.connector import errors

from app.database.connection import DatabaseUnavailableError, MySQLConnectionManager
from app.repositories.vehiculos import Procedure, ProcedureExecutor, VehiculoRepository


class ProtocolCursor:
    """Emula resultados no almacenados; rechaza avanzar sin consumir filas."""

    def __init__(self, connection):
        self.connection = connection
        self.sets = []
        self.index = 0
        self.fetched = False
        self.closed = False
        self.fetches = 0
        self.advances = 0

    def execute(self, statement, parameters=()):
        self.connection.executions.append((statement, parameters))
        script = self.connection.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        self.sets = script
        self.connection.unread = True

    @property
    def with_rows(self):
        return self.sets[self.index] is not None

    def fetchall(self):
        if not self.with_rows:
            raise AssertionError("No se debe pedir filas al resultado final sin columnas")
        self.fetched = True
        self.fetches += 1
        return self.sets[self.index]

    def nextset(self):
        self.advances += 1
        if self.with_rows and not self.fetched:
            raise AssertionError("Faltan consumir filas, incluso si el conjunto está vacío")
        self.fetched = False
        if self.index + 1 == len(self.sets):
            self.connection.unread = False
            return None
        self.index += 1
        return True

    def close(self):
        self.closed = True


class ProtocolConnection:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.executions = []
        self.cursors = []
        self.unread = False
        self.commit = Mock()
        self.rollback = Mock()

    def cursor(self, *, dictionary=False):
        if self.unread:
            raise AssertionError("No se puede reutilizar una conexión con resultados pendientes")
        if not dictionary:
            raise AssertionError("El contrato requiere columnas por nombre")
        cursor = ProtocolCursor(self)
        self.cursors.append(cursor)
        return cursor


class ProcedureExecutorTests(unittest.TestCase):
    def setUp(self):
        # Sólo se utiliza manager.cursor(); no se lee Settings ni .env.
        self.manager = MySQLConnectionManager()
        self.executor = ProcedureExecutor(self.manager)

    def test_allowlist_and_every_arity_match_saved_contract(self):
        path = Path(__file__).resolve().parents[1] / "docs" / "contrato_rds.json"
        procedures = json.loads(path.read_text(encoding="utf-8"))["procedures"]
        expected = {item["name"]: len(item["parameters"]) for item in procedures}
        self.assertEqual({item.value for item in Procedure}, set(expected))
        self.assertEqual(len(Procedure), 21)
        for procedure in Procedure:
            with self.subTest(procedure=procedure.value):
                connection = ProtocolConnection([[None]])
                parameters = (1,) * expected[procedure.value]
                self.executor.execute(connection, procedure, parameters)
                self.assertEqual(connection.executions[0][1], parameters)
                with self.assertRaises(ValueError):
                    self.executor.execute(connection, procedure, parameters + (1,))

    def test_rejects_external_procedure_name_before_cursor_creation(self):
        connection = ProtocolConnection([])
        for value in ("sp_obtener_vehiculos", "sp_obtener_vehiculos`; DROP TABLE vehiculos;--", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.executor.execute(connection, value)
        self.assertEqual(connection.cursors, [])

    def test_values_are_bound_and_never_interpolated(self):
        hostile_value = "x'); DROP TABLE vehiculos; --"
        parameters = (hostile_value, None, "descripción", None)
        connection = ProtocolConnection([[[{"id_vehiculo": 12}], None]])
        identifier = self.executor.identifier(connection, Procedure.CREAR_VEHICULO, parameters, "id_vehiculo")
        self.assertEqual(identifier, 12)
        statement, actual_parameters = connection.executions[0]
        self.assertEqual(statement, "CALL `sp_crear_vehiculo`(%s, %s, %s, %s)")
        self.assertNotIn(hostile_value, statement)
        self.assertIs(actual_parameters, parameters)

    def test_consumes_empty_and_nonempty_sets_and_final_status_before_reuse(self):
        connection = ProtocolConnection([
            [[], [{"extra": 1}], [], None],
            [[{"id_vehiculo": 2}], None],
        ])
        first = self.executor.execute(connection, Procedure.LISTAR_VEHICULOS)
        self.assertEqual(first, [[], [{"extra": 1}], []])
        self.assertEqual(connection.cursors[0].fetches, 3)
        self.assertEqual(connection.cursors[0].advances, 4)
        self.assertTrue(connection.cursors[0].closed)
        self.assertEqual(self.executor.rows(connection, Procedure.LISTAR_VEHICULOS), [{"id_vehiculo": 2}])
        self.assertFalse(connection.unread)
        connection.commit.assert_not_called()
        connection.rollback.assert_not_called()

    def test_rows_preserves_empty_first_set(self):
        connection = ProtocolConnection([[[], [{"should_not_replace_empty": True}], None]])
        self.assertEqual(self.executor.rows(connection, Procedure.LISTAR_VEHICULOS), [])

    def test_bad_insert_result_is_sanitized(self):
        for rows in ([], [{"id_vehiculo": 0}], [{"id_vehiculo": True}], [{"other": 1}]):
            with self.subTest(rows=rows):
                connection = ProtocolConnection([[rows, None]])
                with self.assertRaises(DatabaseUnavailableError) as result:
                    self.executor.identifier(connection, Procedure.CREAR_VEHICULO,
                                             ("name", None, None, None), "id_vehiculo")
                self.assertEqual(result.exception.code, "query")
                self.assertTrue(connection.cursors[0].closed)

    def test_connector_error_is_sanitized_and_cursor_closes(self):
        connection = ProtocolConnection([errors.ProgrammingError("password endpoint user", errno=1142)])
        with self.assertRaises(DatabaseUnavailableError) as result:
            self.executor.execute(connection, Procedure.LISTAR_VEHICULOS)
        self.assertEqual(result.exception.code, "permission")
        self.assertNotIn("password", str(result.exception))
        self.assertTrue(connection.cursors[0].closed)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = VehiculoRepository(MySQLConnectionManager())

    def test_history_identifier_uses_same_connection_after_call_and_never_commits(self):
        connection = ProtocolConnection([[None], [[{"id_historial": 88}]]])
        self.assertEqual(self.repository.registrar_historial(connection, 1, 9, "HTTP"), 88)
        self.assertEqual(connection.executions, [
            ("CALL `sp_registrar_historial`(%s, %s, %s)", (1, 9, "HTTP")),
            ("SELECT LAST_INSERT_ID() AS id_historial", ()),
        ])
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))
        self.assertFalse(connection.unread)
        connection.commit.assert_not_called()
        connection.rollback.assert_not_called()

    def test_empty_vehicle_is_none_but_empty_collection_is_list(self):
        connection = ProtocolConnection([[[], None], [[], None], [[], None]])
        self.assertIsNone(self.repository.obtener_vehiculo(connection, 1))
        self.assertEqual(self.repository.obtener_comandos(connection, 1), [])
        self.assertIsNone(self.repository.obtener_estado(connection, 1))

    def test_history_keeps_all_rows_for_service_to_limit_without_changing_rds(self):
        rows = [{"id_historial": value} for value in range(15, 0, -1)]
        connection = ProtocolConnection([[rows, None]])
        self.assertEqual(self.repository.obtener_historial(connection, 1), rows)

    def test_movement_creation_respects_no_identifier_output(self):
        connection = ProtocolConnection([[None]])
        self.assertIsNone(self.repository.crear_movimiento_demo(connection, 3, 4, 1, 500, 0))
        self.assertEqual(connection.executions, [
            ("CALL `sp_agregar_movimiento_demo`(%s, %s, %s, %s, %s)", (3, 4, 1, 500, 0)),
        ])
        connection.commit.assert_not_called()

    def test_state_lock_returns_current_boolean_and_drains_before_next_lock(self):
        connection = ProtocolConnection([[[{"conectado": False}]], [[]]])
        self.assertEqual(self.repository.bloquear_estado(connection, 1), {"conectado": False})
        self.assertIsNone(self.repository.bloquear_comando(connection, 9))
        self.assertEqual(connection.executions[0], (
            "SELECT conectado FROM estado_vehiculo WHERE id_vehiculo = %s FOR UPDATE", (1,),
        ))
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))

    def test_integrity_and_validation_errors_use_fixed_public_categories(self):
        examples = [
            (errors.IntegrityError("secret duplicate value", errno=1062), "conflict"),
            (errors.IntegrityError("secret child", errno=1451), "conflict"),
            (errors.IntegrityError("secret parent", errno=1452), "conflict"),
            (errors.IntegrityError("secret integrity"), "conflict"),
            (errors.DataError("secret overflow", errno=1264), "validation"),
            (errors.DatabaseError("secret truncated", errno=1265), "validation"),
            (errors.DatabaseError("secret value", errno=1366), "validation"),
            (errors.DataError("secret length", errno=1406), "validation"),
            (errors.IntegrityError("secret check", errno=3819), "validation"),
        ]
        for original, category in examples:
            with self.subTest(category=category, errno=original.errno):
                connection = ProtocolConnection([original])
                with self.assertRaises(DatabaseUnavailableError) as result:
                    self.repository.listar_vehiculos(connection)
                self.assertEqual(result.exception.code, category)
                self.assertNotIn("secret", str(result.exception))


if __name__ == "__main__":
    unittest.main()
