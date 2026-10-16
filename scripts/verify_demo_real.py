"""Verificación autorizada de UNA DEMO real. No repite movimientos individuales.

El journal se crea en la precomprobación y bloquea cualquier segundo intento
de inicio, incluso si la respuesta del primero se pierde. Nunca imprime claves.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

from websockets.asyncio.client import connect

from app.config.settings import Settings
from app.database.connection import MySQLConnectionManager
from app.repositories.vehiculos import VehiculoRepository
from app.security import ApiKeySettings

REPORT_PATH = Path(__file__).resolve().parents[1] / "docs" / "verificacion_demo_real.json"
BASE = "http://127.0.0.1:8000"
ACTIVE_WRITER = None


def now():
    return datetime.now(timezone.utc).isoformat()


def serializable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


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


def save(report):
    if ACTIVE_WRITER is not None:
        ACTIVE_WRITER.write(report)
        return
    # Compatibilidad para revisar evidencia existente, sin renombrar ni reemplazar.
    with REPORT_PATH.open("r+", encoding="utf-8") as output:
        output.seek(0)
        json.dump(report, output, ensure_ascii=False, indent=2, default=str)
        output.write("\n")
        output.truncate()
        output.flush()
        os.fsync(output.fileno())


def snapshot():
    # Nueva Settings y NUEVA conexión/snapshot en cada llamada, incluido el after.
    settings = Settings()
    manager = MySQLConnectionManager(settings)
    repository = VehiculoRepository(manager)
    rds = settings.mysql_host.lower().rstrip(".").endswith((".rds.amazonaws.com", ".rds.amazonaws.com.cn"))
    with manager.transaction(readonly=True) as connection:
        with manager.cursor(connection, dictionary=True) as cursor:
            cursor.execute("SELECT DATABASE() AS schema_name")
            schema = cursor.fetchone()["schema_name"]
            cursor.fetchall()
            cursor.execute("SELECT * FROM historial_comandos WHERE id_vehiculo=%s ORDER BY id_historial", (1,))
            rows = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) AS count, MAX(id_historial) AS max_id FROM historial_comandos WHERE id_vehiculo=%s", (1,))
            counts = cursor.fetchone()
            cursor.fetchall()
        state = repository.obtener_estado(connection, 1)
        history_join = repository.obtener_historial(connection, 1)
        movements = repository.obtener_movimientos_demo(connection, 1)
        demos = repository.obtener_demos(connection, 1)
    return serializable({"rds": rds, "schema": schema, "rows": rows,
                         "counts": counts, "state": state, "history_join": history_join,
                         "movements": movements, "demo_belongs": any(d["id_demo"] == 1 for d in demos)})


def http_get(path):
    with urlopen(BASE + path, timeout=20) as response:
        return json.load(response)


def post_once(private_key):
    request = Request(BASE + "/vehiculos/1/demos/1/iniciar", data=b"",
                      headers={"X-API-Key": private_key}, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        # No reflejar respuestas arbitrarias ni detalles internos del servidor.
        return error.code, None


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_client(client, execution_id, expected):
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
        e["payload"].get("id_vehiculo") == 1 and e["payload"].get("id_demo") == 1
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


async def verify(report):
    before = await asyncio.to_thread(snapshot)
    if (not before["rds"] or before["schema"] != "vehiculo_iot"
            or before["rows"] != report["history_before"]["rows"]):
        raise RuntimeError("preflight_changed")
    state = before["state"]
    record = next((r for r in before["history_join"] if r["id_historial"] == 6), None)
    if not (state and (not report.get("require_initial_avanzar", True)
                      or state["comando"].upper() == "AVANZAR") and record
            and record["comando"].upper() == "AVANZAR" and before["demo_belongs"]):
        raise RuntimeError("persistence_missing")
    api_state, api_history = await asyncio.gather(
        asyncio.to_thread(http_get, "/vehiculos/1/estado"),
        asyncio.to_thread(http_get, "/vehiculos/1/historial"),
    )
    report["api_persistence_matches_fresh_rds"] = (
        all(api_state[f] == state[f] for f in ("id_comando", "comando", "codigo", "conectado"))
        and any(r["id_historial"] == 6 and r["comando"].upper() == "AVANZAR" for r in api_history)
    )
    if not report["api_persistence_matches_fresh_rds"]:
        raise RuntimeError("api_rds_mismatch")
    expected = before["movements"]
    if not expected or expected != report["expected_demo_movements"]:
        raise RuntimeError("demo_changed")
    report["history_before"]["sha256"] = fingerprint(before["rows"])
    report["planned_wait_ms"] = sum(m["duracion_ms"] + m["pausa_ms"] for m in expected)
    private_key = ApiKeySettings().api_key_operator.get_secret_value()
    if not private_key:
        raise RuntimeError("private_auth_unavailable")
    url = "ws://127.0.0.1:8000/ws/vehiculos/1"
    async with connect(url, open_timeout=20, close_timeout=2) as first:
        async with connect(url, open_timeout=20, close_timeout=2) as second:
            sockets = (first, second)
            for index, socket in enumerate(sockets, 1):
                initial = json.loads(await asyncio.wait_for(socket.recv(), 20))
                if not (initial["tipo"] == "estado_inicial" and initial["id_vehiculo"] == 1
                        and initial["id_comando"] == state["id_comando"]
                        and initial["comando"] == state["comando"]
                        and initial["codigo"] == state["codigo"]
                        and initial["conectado"] == bool(state["conectado"])
                        and datetime.fromisoformat(initial["fecha_hora"].replace("Z", "+00:00")).tzinfo is not None
                        and timestamp(initial["fecha_hora"]) == timestamp(state["fecha_actualizacion"])):
                    raise RuntimeError("initial_state_mismatch")
                report["websocket_clients"].append({"client": index, "initial_state": initial,
                                                     "initial_ok": True, "events": []})
            epoch = time.monotonic()
            deadline = max(90, report["planned_wait_ms"] / 1000 + len(expected) * 15 + 20)
            terminal_received = [asyncio.Event(), asyncio.Event()]

            async def collect(index, socket):
                client = report["websocket_clients"][index]
                try:
                    while True:
                        payload = json.loads(await socket.recv())
                        client["events"].append({"payload": payload, "received_at_utc": now(),
                                                  "received_monotonic_ms": round((time.monotonic() - epoch) * 1000, 3)})
                        # Solo memoria: ningún guardado ni fsync interrumpe este lector.
                        if (payload.get("id_ejecucion") == report.get("execution_id")
                                and payload.get("tipo") in {"demo_fin", "demo_error", "demo_cancelada"}):
                            terminal_received[index].set()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    client["reader_error"] = type(error).__name__
                    terminal_received[index].set()

            collectors = []
            try:
                # Journal durable ANTES del único envío. No hay reintentos ni fallback de claves.
                report["post_attempts"] = 1
                report["post"] = {"method": "POST", "path": "/vehiculos/1/demos/1/iniciar",
                                  "attempted_at_utc": now(), "http_status": None}
                report["status"] = "post_attempted"
                save(report)
                collectors = [asyncio.create_task(collect(i, socket)) for i, socket in enumerate(sockets)]
                print("Salida comprobada; historial copiado; dos estados iniciales recibidos. Inicio DEMO:un solo POST.", flush=True)
                status, body = await asyncio.to_thread(post_once, private_key)
                report["post"]["http_status"] = status
                if status != 202 or not body or not body.get("id_ejecucion"):
                    raise RuntimeError("post_not_accepted")
                report["post"]["response"] = body
                try:
                    report["execution_id"] = str(UUID(body["id_ejecucion"]))
                except (TypeError, ValueError, AttributeError):
                    raise RuntimeError("invalid_execution_id") from None
                if body.get("id_vehiculo") != 1 or body.get("id_demo") != 1 or body.get("estado") != "iniciada":
                    raise RuntimeError("accepted_metadata_mismatch")
                if body.get("total_movimientos") != len(expected):
                    raise RuntimeError("accepted_count_mismatch")
                print(f"HTTP202; UUID:{report['execution_id']}; N esperado:{len(expected)}.", flush=True)
                # Si el terminal llegó antes de responder el POST, ya está en memoria.
                for index, client in enumerate(report["websocket_clients"]):
                    if any(e["payload"].get("id_ejecucion") == report["execution_id"]
                           and e["payload"].get("tipo") in {"demo_fin", "demo_error", "demo_cancelada"}
                           for e in client["events"]):
                        terminal_received[index].set()
                async with asyncio.timeout(deadline):
                    await asyncio.gather(*(event.wait() for event in terminal_received))
                if any(client.get("reader_error") for client in report["websocket_clients"]):
                    raise RuntimeError("reader_failed")
            finally:
                for task in collectors:
                    if not task.done():
                        task.cancel()  # Solo lectores WS; nunca tarea DEMO ni operación RDS.
                await asyncio.gather(*collectors, return_exceptions=True)
    for client in report["websocket_clients"]:
        validate_client(client, report["execution_id"], expected)
    report["events_both_clients_ok"] = all(c["ok"] for c in report["websocket_clients"])
    report["same_execution_events_both_clients"] = (
        [e["payload"] for e in report["websocket_clients"][0]["execution_events"]]
        == [e["payload"] for e in report["websocket_clients"][1]["execution_events"]]
    )
    if not report["events_both_clients_ok"] or not report["same_execution_events_both_clients"]:
        raise RuntimeError("event_validation_failed")


def run_existing_report():
    if not REPORT_PATH.exists():
        print("Falta la copia y confirmacion previa: no se envia ningun POST.")
        return 1
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if report.get("post_attempts", 0) != 0 or report.get("status") != "preflight_confirmed":
        print("La evidencia impide repetir el POST. Revisar el resultado existente.")
        return 1
    try:
        asyncio.run(verify(report))
    except Exception as error:
        allowed = {"preflight_changed", "persistence_missing", "api_rds_mismatch", "demo_changed", "reader_failed",
                   "private_auth_unavailable", "initial_state_mismatch", "post_not_accepted",
                   "accepted_count_mismatch", "event_validation_failed", "invalid_execution_id", "accepted_metadata_mismatch"}
        code = str(error) if isinstance(error, RuntimeError) and str(error) in allowed else type(error).__name__
        report["errors"].append({"phase": "verification", "code": code,
                                 "message": "La verificacion real no se completo; no se repite el POST."})
    try:
        after = snapshot()
        report["history_after"] = {"count": len(after["rows"]), "max_id": after["counts"]["max_id"],
                                   "rows": after["rows"], "sha256": fingerprint(after["rows"])}
        report["history_identical"] = after["rows"] == report["history_before"]["rows"]
        report["final_state"] = after["state"]
        expected = report["expected_demo_movements"]
        report["final_state_matches_last_demo_command"] = bool(
            expected and after["state"] and after["state"]["id_comando"] == expected[-1]["id_comando"]
        )
        report["connected_preserved"] = after["state"]["conectado"] == report["persistence"]["state"]["conectado"]
    except Exception:
        report["errors"].append({"phase": "history_after", "code": "read_failed",
                                 "message": "No se pudo comparar el historial mediante una conexion nueva."})
    report["status"] = "success" if (
        not report["errors"] and len(report["websocket_clients"]) == 2 and report.get("events_both_clients_ok")
        and report.get("same_execution_events_both_clients") and report.get("history_identical")
        and report.get("final_state_matches_last_demo_command") and report.get("connected_preserved")
    ) else "failed"
    report["finished_at_utc"] = now()
    report["individual_movement_posts_sent"] = 0
    report["server_left_running"] = True
    save(report)
    print(f"Resultado real:{report['status']}; POST inicio enviados:{report['post_attempts']}; movimiento individual:0.")
    print("Evento/orden             Comando             Duracion   Pausa   Cliente1 Cliente2")
    initial_flags = [c.get("initial_ok", False) for c in report["websocket_clients"]]
    print("estado_inicial          (estado RDS)                              " + str(initial_flags))
    print("demo_inicio             (UUID de arriba)                         "
          + " ".join(str(any(e['payload']['tipo']=='demo_inicio' for e in c['events'])) for c in report['websocket_clients']))
    for movement in report["expected_demo_movements"]:
        flags = [any(e['payload'].get('tipo')=='demo_movimiento' and e['payload'].get('orden')==movement['orden']
                     for e in c['events']) for c in report['websocket_clients']]
        flags = (flags + [False, False])[:2]
        print(f"movimiento {movement['orden']:<12} {movement['comando']:<19} {movement['duracion_ms']:>5}ms {movement['pausa_ms']:>5}ms  {str(flags[0]):<8} {flags[1]}")
    print("demo_fin                                                        "
          + " ".join(str(c.get('demo_fin_received',False)) for c in report['websocket_clients']))
    for client in report["websocket_clients"]:
        events = client.get("execution_events", client["events"])
        print(f"Cliente{client['client']}:" + ",".join(e['payload']['tipo'] for e in events))
        if len(client.get("timing_checks", [])) == len(report["expected_demo_movements"]):
            last = client["timing_checks"][-1]
            print(f"Cliente{client['client']} ultima espera hasta demo_fin:{last['observed_gap_ms']}ms; minimo:{last['expected_wait_ms']}ms; verificada:{last['respected']}.")
    before, after = report["history_before"], report.get("history_after", {})
    print(f"Historial antes COUNT={before['count']} MAX={before['max_id']}; despues COUNT={after.get('count')} MAX={after.get('max_id')}; filas identicas:{report.get('history_identical', False)}.")
    print("Archivo guardado:" + str(REPORT_PATH.resolve()) + ". Servidor existente conservado.")
    return 0 if report["status"] == "success" else 1


def main():
    global REPORT_PATH, ACTIVE_WRITER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    parser.add_argument("--new-execution", action="store_true", help="Autorizar una ejecución nueva en un archivo nuevo.")
    args = parser.parse_args()
    REPORT_PATH = args.output.resolve()
    if not args.new_execution:
        return run_existing_report()
    try:
        ACTIVE_WRITER = EvidenceWriter(REPORT_PATH)
    except OSError as error:
        print(f"Salida no disponible ({type(error).__name__}); POST enviados:0.")
        return 1
    try:
        before = snapshot()
        report = {"started_at_utc": now(), "status": "preflight_confirmed", "id_vehiculo": 1, "id_demo": 1,
                  "target": {"rds_endpoint_confirmed": before["rds"], "schema": before["schema"]},
                  "writer_preflight": ACTIVE_WRITER.preflight,
                  "io_diagnosis": json.loads((REPORT_PATH.parent / 'diagnostico_verificador_io.json').read_text(encoding='utf-8')),
                  "capture_storage": "memory_only_until_both_terminal_events_and_rds_after",
                  "require_initial_avanzar": False, "persistence": {"state": before["state"]},
                  "history_before": {"count": len(before["rows"]), "max_id": before["counts"]["max_id"],
                                     "rows": before["rows"], "sha256": fingerprint(before["rows"])},
                  "expected_demo_movements": before["movements"], "post_attempts": 0,
                  "websocket_clients": [], "errors": []}
        save(report)
        print("Ruta nueva:creacion, escritura, fsync y lectura comprobadas antes del POST.", flush=True)
        return run_existing_report()
    except Exception as error:
        print(f"Precomprobacion/guardado no completado ({type(error).__name__}); no se declara exito ni se repite POST.")
        return 1
    finally:
        ACTIVE_WRITER.close()
        ACTIVE_WRITER = None


if __name__ == "__main__":
    raise SystemExit(main())
