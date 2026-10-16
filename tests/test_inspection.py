"""Validación de redacción usando valores ficticios, sin .env ni conexiones."""

import unittest

from app.config.settings import Settings
from scripts.inspect_rds import quote_identifier, redact_definition


def dummy_settings(password: str = "dummy_secret") -> Settings:
    return Settings(
        _env_file=None,
        mysql_host="db.example.invalid",
        mysql_port=3306,
        mysql_user="practice_user",
        mysql_password=password,
        mysql_database="practice_schema",
    )


class InspectionRedactionTests(unittest.TestCase):
    def test_redacts_plain_connection_values(self):
        settings = dummy_settings()
        source = (
            "CREATE PROCEDURE `demo`() SELECT 'db.example.invalid', "
            "'practice_user', 'dummy_secret';"
        )
        result, annotations = redact_definition(source, settings)
        self.assertEqual(result.count("[REDACTADO]"), 3)
        self.assertEqual(annotations, ["Valores privados omitidos."])

    def test_redacts_mysql_backslash_escaped_password(self):
        password = "dummy'password\\segment"
        escaped = password.replace("\\", "\\\\").replace("'", "\\'")
        result, annotations = redact_definition(
            "CREATE PROCEDURE `demo`() SELECT '" + escaped + "';",
            dummy_settings(password),
        )
        self.assertNotIn(escaped, result)
        self.assertIn("SELECT '[REDACTADO]';", result)
        self.assertIn("Valores privados omitidos.", annotations)

    def test_redacts_doubled_quotes_and_backticks(self):
        for password, escaped in (
            ("dummy'quote", "dummy''quote"),
            ('dummy"quote', 'dummy""quote'),
            ("dummy`quote", "dummy``quote"),
        ):
            with self.subTest(encoding="doubled"):
                result, _ = redact_definition(
                    "CREATE PROCEDURE `demo`() SELECT '" + escaped + "';",
                    dummy_settings(password),
                )
                self.assertNotIn(escaped, result)
                self.assertIn("[REDACTADO]", result)

    def test_redacts_mysql_escaped_control_characters(self):
        password = "dummy\0\b\n\r\t\x1a\"'\\end"
        escaped = "dummy\\0\\b\\n\\r\\t\\Z\\\"\\'\\\\end"
        result, _ = redact_definition(
            "CREATE PROCEDURE `demo`() SELECT '" + escaped + "';",
            dummy_settings(password),
        )
        self.assertIn("SELECT '[REDACTADO]';", result)
        self.assertNotIn(escaped, result)

    def test_redacts_mixed_mysql_quote_encodings(self):
        password = "dummy'a'b\"c\"d"
        escaped = "dummy\\'a''b\\\"c\"\"d"
        result, _ = redact_definition(
            "CREATE PROCEDURE `demo`() SELECT '" + escaped + "';",
            dummy_settings(password),
        )
        self.assertIn("SELECT '[REDACTADO]';", result)

    def test_removes_definer_account(self):
        for account in (
            "`some``account`@`private.host`",
            "'some''account'@'private.host'",
            "some_account@private.host",
        ):
            with self.subTest(quoting=account[0]):
                result, annotations = redact_definition(
                    f"CREATE DEFINER={account} PROCEDURE `demo`() SELECT 1;",
                    dummy_settings(),
                )
                self.assertEqual(result, "CREATE PROCEDURE `demo`() SELECT 1;")
                self.assertEqual(annotations, ["Cuenta DEFINER omitida."])

    def test_leaves_definition_without_private_values_intact(self):
        source = "CREATE PROCEDURE `demo`(IN item_id INT) SELECT item_id;"
        self.assertEqual(redact_definition(source, dummy_settings()), (source, []))

    def test_quotes_entire_identifier_and_escapes_backticks(self):
        self.assertEqual(quote_identifier("vehiculo_iot"), "`vehiculo_iot`")
        self.assertEqual(quote_identifier("demo`; SELECT 1; --"), "`demo``; SELECT 1; --`")


if __name__ == "__main__":
    unittest.main()
