"""Pruebas locales de recursos y errores, sin red ni configuración real."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mysql.connector import errors
from pydantic import SecretStr

from app.database.connection import DatabaseUnavailableError, MySQLConnectionManager


def fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        mysql_host="example.invalid",
        mysql_port=3306,
        mysql_user="example_user",
        mysql_password=SecretStr("example_password"),
        mysql_database="example_database",
    )


class ConnectionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = MySQLConnectionManager(fake_settings())

    @patch("app.database.connection.mysql.connector.connect")
    def test_new_connection_for_each_operation_and_timeout_options(self, connect: Mock) -> None:
        first, second = Mock(), Mock()
        connect.side_effect = [first, second]
        with self.manager.connection() as actual_first:
            self.assertIs(actual_first, first)
        with self.manager.connection() as actual_second:
            self.assertIs(actual_second, second)
        self.assertEqual(connect.call_count, 2)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        options = connect.call_args.kwargs
        self.assertEqual(options["connection_timeout"], 5)
        self.assertEqual(options["read_timeout"], 10)
        self.assertEqual(options["write_timeout"], 10)
        self.assertFalse(options["autocommit"])

    @patch("app.database.connection.mysql.connector.connect")
    def test_health_query_closes_connection_and_cursor(self, connect: Mock) -> None:
        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (1,)
        self.manager.check_connection()
        cursor.execute.assert_called_once_with("SELECT 1 AS ok")
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()
        connection.commit.assert_not_called()

    @patch("app.database.connection.mysql.connector.connect")
    def test_query_error_is_redacted_and_resources_close(self, connect: Mock) -> None:
        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.execute.side_effect = errors.ProgrammingError(
            "example_password example_user example.invalid", errno=1142
        )
        with self.assertRaises(DatabaseUnavailableError) as result:
            self.manager.check_connection()
        self.assertEqual(result.exception.code, "permission")
        self.assertNotIn("example_", str(result.exception))
        self.assertTrue(result.exception.__suppress_context__)
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    @patch("app.database.connection.mysql.connector.connect")
    def test_cursor_close_error_is_redacted_and_connection_still_closes(self, connect: Mock) -> None:
        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (1,)
        cursor.close.side_effect = errors.OperationalError("example_password", errno=2013)
        with self.assertRaises(DatabaseUnavailableError) as result:
            self.manager.check_connection()
        self.assertEqual(result.exception.code, "network")
        self.assertNotIn("example_password", str(result.exception))
        connection.close.assert_called_once_with()

    @patch("app.database.connection.mysql.connector.connect")
    def test_failed_health_result_is_rejected(self, connect: Mock) -> None:
        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = None
        with self.assertRaises(DatabaseUnavailableError) as result:
            self.manager.check_connection()
        self.assertEqual(result.exception.code, "query")
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    @patch("app.database.connection.mysql.connector.connect")
    def test_successful_transaction_commits_and_closes(self, connect: Mock) -> None:
        connection = connect.return_value
        with self.manager.transaction(readonly=True) as actual:
            self.assertIs(actual, connection)
        connection.start_transaction.assert_called_once_with(readonly=True)
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        connection.close.assert_called_once_with()

    @patch("app.database.connection.mysql.connector.connect")
    def test_failed_transaction_rolls_back_and_closes_despite_rollback_error(self, connect: Mock) -> None:
        connection = connect.return_value
        connection.rollback.side_effect = errors.OperationalError("example_password", errno=2013)
        connection.close.side_effect = errors.OperationalError("example_password", errno=2013)
        with self.assertRaisesRegex(ValueError, "business failure"):
            with self.manager.transaction():
                raise ValueError("business failure")
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()
        connection.commit.assert_not_called()

    @patch("app.database.connection.mysql.connector.connect")
    def test_commit_failure_attempts_rollback_and_closes(self, connect: Mock) -> None:
        connection = connect.return_value
        connection.commit.side_effect = errors.OperationalError("example_password", errno=2013)
        with self.assertRaises(DatabaseUnavailableError) as result:
            with self.manager.transaction():
                pass
        self.assertEqual(result.exception.code, "network")
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    @patch("app.database.connection.mysql.connector.connect")
    def test_start_transaction_failure_closes_and_never_yields(self, connect: Mock) -> None:
        connection = connect.return_value
        connection.start_transaction.side_effect = errors.OperationalError("example_password", errno=2013)
        with self.assertRaises(DatabaseUnavailableError):
            with self.manager.transaction():
                self.fail("No debe entregar una transacción que no pudo iniciarse")
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()
        connection.commit.assert_not_called()

    @patch("app.database.connection.mysql.connector.connect")
    def test_permission_error_can_be_handled_and_next_cursor_used(self, connect: Mock) -> None:
        connection = connect.return_value
        denied_cursor, next_cursor = Mock(), Mock()
        connection.cursor.side_effect = [denied_cursor, next_cursor]
        denied_cursor.execute.side_effect = errors.ProgrammingError("example_password", errno=1370)
        with self.manager.transaction(readonly=True) as actual:
            try:
                with self.manager.cursor(actual, dictionary=True) as cursor:
                    cursor.execute("SHOW CREATE PROCEDURE example_procedure")
            except DatabaseUnavailableError as error:
                self.assertEqual(error.code, "permission")
            with self.manager.cursor(actual, dictionary=True) as cursor:
                cursor.execute("SELECT 1")
        denied_cursor.close.assert_called_once_with()
        next_cursor.close.assert_called_once_with()
        connection.commit.assert_called_once_with()

    @patch("app.database.connection.get_settings")
    @patch("app.database.connection.mysql.connector.connect")
    def test_configuration_error_is_redacted_before_connect(self, connect: Mock, get_settings: Mock) -> None:
        get_settings.side_effect = ValueError("example_password")
        with self.assertRaises(DatabaseUnavailableError) as result:
            with MySQLConnectionManager().connection():
                pass
        self.assertEqual(result.exception.code, "configuration")
        self.assertNotIn("example_password", str(result.exception))
        connect.assert_not_called()

    def test_invalid_timeout_rejected_safely(self) -> None:
        for value in (0, -1, True, "5"):
            with self.subTest(value=value):
                with self.assertRaises(DatabaseUnavailableError) as result:
                    MySQLConnectionManager(fake_settings(), connection_timeout=value)
                self.assertEqual(result.exception.code, "configuration")

    def test_connection_error_categories_do_not_expose_original_message(self) -> None:
        examples = [
            (errors.InterfaceError("example_password", errno=2005), "dns"),
            (errors.OperationalError("example_password", errno=1045), "authentication"),
            (errors.ProgrammingError("example_password", errno=1049), "database_missing"),
            (errors.InterfaceError("example_password", errno=2026), "tls"),
            (errors.OperationalError("example_password", errno=2003), "network"),
            (errors.OperationalError("example_password", errno=2055), "network"),
            (errors.OperationalError("example_password timed out", errno=2003), "timeout"),
            (errors.ConnectionTimeoutError("example_password"), "timeout"),
            (errors.ReadTimeoutError("example_password"), "timeout"),
            (errors.WriteTimeoutError("example_password"), "timeout"),
        ]
        for original, expected in examples:
            with self.subTest(category=expected):
                with patch("app.database.connection.mysql.connector.connect", side_effect=original):
                    with self.assertRaises(DatabaseUnavailableError) as result:
                        with self.manager.connection():
                            pass
                self.assertEqual(result.exception.code, expected)
                self.assertNotIn("example_password", str(result.exception))
                self.assertTrue(result.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
