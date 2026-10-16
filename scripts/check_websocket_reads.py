"""Monitor real de solo lectura: dos clientes, reconexión y vehículo inexistente."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.request import urlopen

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.models.events import VehicleEvent

REPORT = Path(__file__).resolve().parents[1] / "docs" / "verificacion_websocket_lectura.json"


def get_json(path):
    with urlopen("http://127.0.0.1:8000" + path, timeout=20) as response:
        return json.load(response)


async def check():
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "status": "failed",
              "writes": 0, "control_posts": 0, "simultaneous_clients": 0,
              "reconnection": False, "unknown_vehicle_close": None, "warnings": []}
    try:
        vehicles = await asyncio.to_thread(get_json, "/vehiculos")
        if not vehicles:
            report["status"] = "partial"
            report["warnings"].append("Sin vehículos para comprobar un monitor real; no se crean datos.")
            return report
        vehicle = vehicles[0]["id_vehiculo"]
        state = await asyncio.to_thread(get_json, f"/vehiculos/{vehicle}/estado")
        url = f"ws://127.0.0.1:8000/ws/vehiculos/{vehicle}"

        async def initial(socket):
            raw = json.loads(await asyncio.wait_for(socket.recv(), 20))
            assert datetime.fromisoformat(raw["fecha_hora"].replace("Z", "+00:00")).tzinfo is not None
            event = VehicleEvent.model_validate(raw)
            assert event.tipo == "estado_inicial" and event.id_vehiculo == vehicle
            assert event.conectado == (state["conectado"] if state else None)
            assert event.id_comando == (state["id_comando"] if state else None)

        async with connect(url, open_timeout=20, close_timeout=2) as first:
            async with connect(url, open_timeout=20, close_timeout=2) as second:
                await asyncio.gather(initial(first), initial(second))
                report["simultaneous_clients"] = 2
                await first.close()
                async with connect(url, open_timeout=20, close_timeout=2) as fresh:
                    await initial(fresh)
                    report["reconnection"] = True
        ids = {v["id_vehiculo"] for v in vehicles}
        missing = next(i for i in range(1, len(ids) + 2) if i not in ids)
        async with connect(f"ws://127.0.0.1:8000/ws/vehiculos/{missing}", open_timeout=20, close_timeout=2) as invalid:
            try:
                await asyncio.wait_for(invalid.recv(), 20)
                raise AssertionError("El vehículo inexistente no debe recibir estado.")
            except ConnectionClosed as error:
                code = error.rcvd.code if error.rcvd else None
                assert code == 4404
                report["unknown_vehicle_close"] = code
        report["status"] = "ok"
    except Exception:
        report["warnings"].append("No se completó la verificación del monitor local; revisa disponibilidad y contrato.")
    return report


def main():
    report = asyncio.run(check())
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Monitor real: {report['status']}; clientes simultaneos: {report['simultaneous_clients']}.")
    print(f"Reconexion: {report['reconnection']}; cierre inexistente: {report['unknown_vehicle_close']}.")
    print("POST de control: 0; escrituras RDS: 0. Evidencia: docs/verificacion_websocket_lectura.json.")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
