"""Pruebas aisladas del verificador: archivos temporales, sin .env, red ni RDS."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_demo_real import EvidenceWriter, validate_client


EXECUTION_ID = "00000000-0000-4000-8000-000000000001"


def valid_capture():
    expected = [
        {"orden": 1, "id_comando": 2, "comando": "AVANZAR", "codigo": 1,
         "duracion_ms": 200, "pausa_ms": 100},
        {"orden": 2, "id_comando": 3, "comando": "RETROCEDER", "codigo": 2,
         "duracion_ms": 300, "pausa_ms": 0},
    ]

    def event(kind, received, movement=None):
        payload = {"tipo": kind, "id_vehiculo": 1, "id_demo": 1,
                   "id_ejecucion": EXECUTION_ID, "total_movimientos": 2,
                   "fecha_hora": "2026-10-16T03:00:00+00:00"}
        payload.update(movement or {})
        return {"payload": payload, "received_monotonic_ms": received}

    return {"initial_ok": True, "events": [
        event("demo_inicio", 0), event("demo_movimiento", 10, expected[0]),
        event("demo_movimiento", 310, expected[1]), event("demo_fin", 610),
    ]}, expected


class EvidenceWriterTests(unittest.TestCase):
    def test_preflight_checks_new_output_before_any_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            writer = EvidenceWriter(path)
            try:
                self.assertTrue(writer.preflight["exclusive_create"])
                self.assertTrue(writer.preflight["flush_fsync"])
                self.assertTrue(writer.preflight["read_back"])
                self.assertTrue(writer.preflight["held_writable_descriptor"])
                self.assertFalse(writer.preflight["replace_existing_file"])
                writer.file.seek(0)
                self.assertEqual(json.load(writer.file)["post_attempts"], 0)
                self.assertFalse(writer.file.closed)
            finally:
                writer.close()

    def test_existing_evidence_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text("previous evidence", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                EvidenceWriter(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous evidence")

    def test_write_uses_held_descriptor_even_with_open_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            writer = EvidenceWriter(path)
            try:
                with path.open("r", encoding="utf-8") as reader:
                    with patch("scripts.verify_demo_real.os.replace",
                               side_effect=AssertionError("Must not replace the destination")):
                        writer.write({"status": "complete", "events": ["demo_fin"]})
                    self.assertEqual(json.load(reader),
                                     {"status": "complete", "events": ["demo_fin"]})
            finally:
                writer.close()

    def test_shorter_final_document_truncates_previous_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            writer = EvidenceWriter(path)
            try:
                writer.write({"status": "x" * 1000})
                writer.write({"status": "ok"})
                writer.file.seek(0)
                self.assertEqual(json.load(writer.file), {"status": "ok"})
            finally:
                writer.close()


class ValidateRealCaptureTests(unittest.TestCase):
    def test_valid_sequence_checks_every_wait_including_last(self):
        client, expected = valid_capture()
        validate_client(client, EXECUTION_ID, expected)
        self.assertTrue(client["ok"])
        self.assertTrue(client["demo_fin_received"])
        self.assertEqual(len(client["timing_checks"]), len(expected))
        self.assertEqual(client["timing_checks"][-1]["expected_wait_ms"], 300)
        self.assertTrue(client["total_wait_check"]["respected"])

    def test_missing_fin_never_passes(self):
        client, expected = valid_capture()
        client["events"].pop()
        validate_client(client, EXECUTION_ID, expected)
        self.assertFalse(client["ok"])
        self.assertFalse(client["demo_fin_received"])

    def test_last_wait_must_finish_before_demo_fin(self):
        client, expected = valid_capture()
        client["events"][-1]["received_monotonic_ms"] = 320
        validate_client(client, EXECUTION_ID, expected)
        self.assertFalse(client["ok"])
        self.assertFalse(client["timing_checks"][-1]["respected"])

    def test_small_per_step_shortfalls_cannot_hide_short_total(self):
        client, expected = valid_capture()
        client["events"][2]["received_monotonic_ms"] = 240
        client["events"][3]["received_monotonic_ms"] = 470
        validate_client(client, EXECUTION_ID, expected)
        self.assertTrue(all(c["respected"] for c in client["timing_checks"]))
        self.assertFalse(client["total_wait_check"]["respected"])
        self.assertFalse(client["ok"])

    def test_rds_movement_fields_and_order_must_match(self):
        for changed_field in ("orden", "id_comando", "comando", "codigo", "duracion_ms", "pausa_ms"):
            with self.subTest(field=changed_field):
                client, expected = valid_capture()
                value = client["events"][1]["payload"][changed_field]
                client["events"][1]["payload"][changed_field] = value + 1 if isinstance(value, int) else "OTHER"
                validate_client(client, EXECUTION_ID, expected)
                self.assertFalse(client["ok"])
                self.assertFalse(client["movement_checks"][0]["matches_rds"])

    def test_missing_or_duplicate_movement_never_passes(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                client, expected = valid_capture()
                if duplicate:
                    client["events"].insert(2, deepcopy(client["events"][1]))
                else:
                    del client["events"][1]
                validate_client(client, EXECUTION_ID, expected)
                self.assertFalse(client["ok"])

    def test_initial_state_and_execution_metadata_are_required(self):
        client, expected = valid_capture()
        client["initial_ok"] = False
        validate_client(client, EXECUTION_ID, expected)
        self.assertFalse(client["ok"])
        for field, value in (("id_vehiculo", 2), ("id_demo", 2),
                             ("total_movimientos", 99), ("fecha_hora", "2026-10-16T03:00:00")):
            with self.subTest(field=field):
                client, expected = valid_capture()
                client["events"][0]["payload"][field] = value
                validate_client(client, EXECUTION_ID, expected)
                self.assertFalse(client["ok"])


if __name__ == "__main__":
    unittest.main()
