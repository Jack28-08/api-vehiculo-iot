"""Compatibilidad del verificador DEMO y utilidades de evidencia.

La CLI delega en el verificador parametrizado 2.6: exige destino, IDs y salida
nuevos. No conserva supuestos sobre seis filas, ID 6 ni un comando previo.
"""

from datetime import datetime
import json
import os
from pathlib import Path

class EvidenceWriter:
    """Archivo nuevo, descriptor retenido; no reemplaza destinos abiertos en Windows."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = self.path.open("x+", encoding="utf-8")
        self.write({"status": "output_preflight", "post_attempts": 0})
        self.file.seek(0)
        if json.load(self.file)["status"] != "output_preflight":
            raise OSError("Falló la lectura de comprobación del archivo nuevo.")
        self.preflight = {"exclusive_create": True, "write": True, "flush_fsync": True,
                          "read_back": True, "held_writable_descriptor": True,
                          "replace_existing_file": False}

    def write(self, report):
        output = self.file
        output.seek(0)
        json.dump(report, output, ensure_ascii=False, indent=2, default=str)
        output.write("\n")
        output.truncate()
        output.flush()
        os.fsync(output.fileno())

    def close(self):
        self.file.close()



def validate_client(client, execution_id, expected, *, vehicle_id=1, demo_id=1):
    events = [item for item in client["events"] if item["payload"].get("id_ejecucion") == execution_id]
    client["execution_events"] = events
    expected_types = ["demo_inicio"] + ["demo_movimiento"] * len(expected) + ["demo_fin"]
    client["sequence_ok"] = [e["payload"]["tipo"] for e in events] == expected_types
    client["movement_checks"] = []
    actual = [item for item in events if item["payload"]["tipo"] == "demo_movimiento"]
    fields = ("orden", "id_comando", "comando", "codigo", "duracion_ms", "pausa_ms")
    for index, reference in enumerate(expected):
        event = actual[index]["payload"] if index < len(actual) else {}
        client["movement_checks"].append({"orden": reference["orden"],
                                          "matches_rds": all(event.get(f) == reference[f] for f in fields)})
    client["metadata_ok"] = bool(events) and all(
        e["payload"].get("id_vehiculo") == vehicle_id and e["payload"].get("id_demo") == demo_id
        and e["payload"].get("total_movimientos") == len(expected)
        and datetime.fromisoformat(e["payload"]["fecha_hora"].replace("Z", "+00:00")).tzinfo is not None
        for e in events
    )
    # Monotónico de recepción: las marcas SQL tienen otra precisión y no son cronómetro.
    client["timing_checks"] = []
    client["timing_tolerance_ms"] = 100
    for index, reference in enumerate(expected[:len(actual)]):
        if index + 1 < len(actual) or client["sequence_ok"]:
            current = actual[index]
            following = actual[index + 1] if index + 1 < len(actual) else events[-1]
            observed = following["received_monotonic_ms"] - current["received_monotonic_ms"]
            minimum = reference["duracion_ms"] + reference["pausa_ms"]
            client["timing_checks"].append({"orden": reference["orden"], "expected_wait_ms": minimum,
                                            "observed_gap_ms": round(observed, 3),
                                            "respected": observed + 100 >= minimum})
    client["ok"] = (client["sequence_ok"] and client["metadata_ok"]
                    and client.get("initial_ok", False)
                    and len(client["movement_checks"]) == len(actual) == len(expected)
                    and len(client["timing_checks"]) == len(expected)
                    and all(c["matches_rds"] for c in client["movement_checks"])
                    and all(c["respected"] for c in client["timing_checks"]))
    client["demo_fin_received"] = bool(events and events[-1]["payload"]["tipo"] == "demo_fin")
    if client["sequence_ok"] and actual:
        observed = events[-1]["received_monotonic_ms"] - actual[0]["received_monotonic_ms"]
        minimum = sum(m["duracion_ms"] + m["pausa_ms"] for m in expected)
        client["total_wait_check"] = {"expected_min_ms": minimum, "observed_ms": round(observed, 3),
                                      "respected": observed + 100 >= minimum}
        client["ok"] = client["ok"] and client["total_wait_check"]["respected"]


def main(argv=None):
    # Importacion diferida para mantener utilidades de pruebas sin red/configuracion.
    import argparse
    from scripts.verify_public import main as public_main
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--ws-base-url")
    parser.add_argument("--vehicle-id", required=True, type=int)
    parser.add_argument("--demo-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--new-execution", action="store_true", help="Autoriza iniciar UNA DEMO real.")
    parser.add_argument("--confirm-target", required=True, help="Repite exactamente el destino autorizado.")
    parser.add_argument("--allow-http", action="store_true")
    args = parser.parse_args(argv)
    if not args.new_execution:
        parser.error("Se requiere --new-execution; no se reanuda ni repite una evidencia existente.")
    forwarded = ["--scenario", "demo", "--base-url", args.base_url,
                 "--vehicle-id", str(args.vehicle_id), "--demo-id", str(args.demo_id),
                 "--output", str(args.output), "--allow-writes", "--rds-readback",
                 "--confirm-target", args.confirm_target]
    if args.ws_base_url:
        forwarded += ["--ws-base-url", args.ws_base_url]
    if args.allow_http:
        forwarded += ["--allow-http"]
    return public_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
