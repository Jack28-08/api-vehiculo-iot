"""Pruebas del aprovisionamiento del catálogo con un transporte local falso."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import catalogo_diagonales as script


BASE = "https://catalogo.invalid"
PRIVATE_KEY = "clave-ficticia-admin"


def command(identifier, code, name=None, *, description=None, active=True):
    return {
        "id_comando": identifier, "id_vehiculo": 1, "codigo": code,
        "nombre": name or script.DIAGONALS[code], "descripcion": description, "activo": active,
    }


class CatalogDouble:
    def __init__(self, entries=()):
        self.rows = {row["id_comando"]: dict(row) for row in entries}
        self.next_identifier = 101
        self.ordered = []
        self.key = None
        self.lose_response = False
        self.do_not_commit = False
        self.last_attempt = None

    def get(self, path):
        if path != script.PATH:
            raise AssertionError(path)
        self.ordered.append(("GET", path))
        return [dict(row) for row in self.rows.values()]

    def insert(self, body):
        identifier = self.next_identifier
        self.next_identifier += 1
        row = command(identifier, body["codigo"], body["nombre"], description=body["descripcion"])
        self.rows[identifier] = row
        return row

    def request(self, method, path, body, *, expect):
        if method != "POST" or expect is not None or self.key != PRIVATE_KEY:
            raise AssertionError("Se esperaba un POST autorizado.")
        self.ordered.append(("POST", path))
        if path == script.PATH:
            self.last_attempt = dict(body)
            if self.do_not_commit:
                raise TimeoutError(PRIVATE_KEY)
            for row in self.rows.values():
                if row["codigo"] == body["codigo"] or row["nombre"] == body["nombre"]:
                    return 409, None
            created = self.insert(body)
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError(PRIVATE_KEY)
            return 201, dict(created)
        identifier = int(path.removeprefix(script.PATH + "/").removesuffix("/actualizar"))
        row = self.rows[identifier]
        row.update(body)
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError(PRIVATE_KEY)
        return 200, dict(row)

    @property
    def writes(self):
        return [path for method, path in self.ordered if method == "POST"]


class CatalogoDiagonalesTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.journal = Path(tmp) / "diagonales.json"

    def invoke(self, api, mode=None, base=BASE):
        args = ["--base-url", base]
        if mode:
            args.extend((mode, "--journal", str(self.journal)))
        output = StringIO()
        with (patch.object(script, "HttpClient", return_value=api),
              patch.object(script.getpass, "getpass", return_value=PRIVATE_KEY) as prompt,
              redirect_stdout(output)):
            result = script.main(args)
        self.assertIsNone(api.key)
        if self.journal.exists():
            self.assertNotIn(PRIVATE_KEY, self.journal.read_text(encoding="utf-8"))
        self.assertNotIn(PRIVATE_KEY, output.getvalue())
        return result, output.getvalue(), prompt.call_count

    def test_default_reads_catalog_and_reports_real_ids(self):
        api = CatalogDouble([command(83, 9)])
        result, output, prompts = self.invoke(api)
        self.assertEqual((result, prompts), (0, 0))
        self.assertIn("ID 83, activo", output)
        self.assertIn("12 RETROCEDER_DERECHA · faltante", output)
        self.assertEqual(api.ordered, [("GET", script.PATH)])
        self.assertFalse(self.journal.exists())

    def test_code_and_name_collisions_abort_before_prompt_or_post(self):
        collisions = (
            command(83, 9, "OTRO_MOVIMIENTO"),
            command(84, 13, "avanzar_izquierda"),
            command(85, 15, "RETROCEDER_DERÉCHA"),
        )
        for row in collisions:
            with self.subTest(row=row["id_comando"]):
                api = CatalogDouble([row])
                result, output, prompts = self.invoke(api, "--apply")
                self.assertEqual((result, prompts), (1, 0))
                self.assertIn("Colisión", output)
                self.assertIn(str(row["id_comando"]), output)
                self.assertEqual(api.writes, [])
                self.assertFalse(self.journal.exists())

    def test_apply_is_idempotent_and_preserves_preexisting_rows(self):
        existing = command(46, 10, description="Preexistente", active=False)
        api = CatalogDouble([existing])
        result, output, prompts = self.invoke(api, "--apply")
        self.assertEqual((result, prompts), (0, 1))
        self.assertIn("ID 46, inactivo", output)
        stored = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(stored["rows"], {"9": 101, "11": 102, "12": 103})
        self.assertEqual(api.writes, [script.PATH] * 3)
        result, _, prompts = self.invoke(api, "--apply")
        self.assertEqual((result, prompts), (0, 0))
        self.assertEqual(api.writes, [script.PATH] * 3)
        self.assertEqual(api.rows[46], existing)

    def test_timeout_after_commit_recovers_id_from_read(self):
        api = CatalogDouble()
        api.lose_response = True
        self.assertEqual(self.invoke(api, "--apply")[0], 0)
        stored = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(stored["rows"], {"9": 101, "10": 102, "11": 103, "12": 104})
        self.assertEqual(api.writes, [script.PATH] * 4)

    def test_resume_with_same_journal_after_ambiguous_partial_request(self):
        api = CatalogDouble()
        api.do_not_commit = True
        self.assertEqual(self.invoke(api, "--apply")[0], 1)
        pending = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(pending["rows"], {"9": None})
        api.insert(api.last_attempt)
        api.do_not_commit = False
        self.assertEqual(self.invoke(api, "--apply")[0], 0)
        self.assertEqual(api.writes, [script.PATH] * 4)
        completed = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(completed["rows"], {"9": 101, "10": 102, "11": 103, "12": 104})

    def test_restore_disables_only_owned_ids_and_handles_lost_response(self):
        existing = command(46, 10, description="Preexistente")
        api = CatalogDouble([existing])
        self.assertEqual(self.invoke(api, "--apply")[0], 0)
        api.lose_response = True
        self.assertEqual(self.invoke(api, "--restore")[0], 0)
        self.assertEqual(api.rows[46], existing)
        for identifier in (101, 102, 103):
            self.assertFalse(api.rows[identifier]["activo"])
        self.assertEqual(api.writes[3:], [f"{script.PATH}/{i}/actualizar" for i in (101, 102, 103)])
        count = len(api.writes)
        result, _, prompts = self.invoke(api, "--restore")
        self.assertEqual((result, prompts), (0, 0))
        self.assertEqual(len(api.writes), count)

    def test_restore_aborts_if_row_was_changed_or_id_replaced(self):
        api = CatalogDouble()
        self.assertEqual(self.invoke(api, "--apply")[0], 0)
        api.rows[101] = {**api.rows[101], "id_comando": 98}
        result, output, prompts = self.invoke(api, "--restore")
        self.assertEqual((result, prompts), (1, 0))
        self.assertIn("cambió o pertenece a otra operación", output)
        self.assertEqual(api.writes, [script.PATH] * 4)

    def test_restore_can_disable_confirmed_ids_with_one_unresolved_insert(self):
        journal = script.Journal(self.journal, BASE)
        journal.record(9, 101)
        journal.record(10, None)
        api = CatalogDouble([command(101, 9, description=journal.description(9))])
        result, output, prompts = self.invoke(api, "--restore")
        self.assertEqual((result, prompts), (1, 1))
        self.assertFalse(api.rows[101]["activo"])
        self.assertIn("Altas sin confirmar para los códigos 10", output)

    def test_journal_cannot_restore_against_another_base_url(self):
        api = CatalogDouble()
        self.assertEqual(self.invoke(api, "--apply")[0], 0)
        result, output, prompts = self.invoke(api, "--restore", base="https://otro-catalogo.invalid")
        self.assertEqual((result, prompts), (1, 0))
        self.assertIn("corresponde a otro destino", output)
        self.assertEqual(api.writes, [script.PATH] * 4)

    def test_lock_excludes_a_second_writer_and_releases_when_closed(self):
        with script.reserve_journal(self.journal):
            with self.assertRaises(script.CatalogError):
                with script.reserve_journal(self.journal):
                    self.fail("Se aceptó una escritura concurrente con el mismo diario.")
        with script.reserve_journal(self.journal):
            pass

    def test_cleartext_remote_target_is_rejected_before_http_client(self):
        with (patch.object(script, "HttpClient") as transport,
              redirect_stderr(StringIO()), self.assertRaises(SystemExit)):
            script.main(["--base-url", "http://catalogo.invalid"])
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
