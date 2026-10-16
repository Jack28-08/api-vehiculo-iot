"""Comprobación HTTP real de solo lectura; nunca envía POST ni claves API."""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:8000"
REPORT_PATH = Path(__file__).resolve().parents[1] / "docs" / "verificacion_http_get.json"


def main() -> int:
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "failed", "read_only": True, "writes": 0,
        "requests": [], "functional_get_kinds_checked": 0, "warnings": [],
    }
    kinds = set()

    def get(path: str, expected: int = 200, *, is_json: bool = True):
        request = Request(BASE_URL + path, method="GET")
        try:
            with urlopen(request, timeout=60) as response:
                status, body = response.status, response.read()
        except HTTPError as error:
            status, body = error.code, error.read()
        entry = {"method": "GET", "path": path, "status": status}
        report["requests"].append(entry)
        if status != expected:
            raise RuntimeError(f"GET {path}: HTTP {status}; esperado {expected}.")
        value = json.loads(body) if is_json else body.decode("utf-8")
        if isinstance(value, list):
            entry["items"] = len(value)
        elif value is None:
            entry["empty_state"] = True
        return value

    try:
        assert get("/health") == {"status": "ok"}
        assert get("/health/db") == {"status": "ok"}
        assert "SwaggerUIBundle" in get("/docs", is_json=False)
        schema = get("/openapi.json")
        assert schema["components"]["securitySchemes"]["ApiKey"]["name"] == "X-API-Key"
        for path, operations in schema["paths"].items():
            if "post" in operations:
                assert operations["post"].get("security"), f"POST sin seguridad: {path}"
        vehicles = get("/vehiculos")
        kinds.add("vehicles")
        demo_owner = None
        # Hasta tres vehículos completos: ejercicio reproducible sin recorrer una base grande.
        for vehicle in vehicles[:3]:
            identifier = vehicle["id_vehiculo"]
            prefix = f"/vehiculos/{identifier}"
            assert get(prefix)["id_vehiculo"] == identifier
            kinds.add("vehicle")
            commands = get(prefix + "/comandos")
            assert all(c["id_vehiculo"] == identifier for c in commands)
            kinds.add("commands")
            state = get(prefix + "/estado")
            assert state is None or state["id_vehiculo"] == identifier
            kinds.add("state")
            history = get(prefix + "/historial")
            assert len(history) <= 10
            assert all(h["id_vehiculo"] == identifier for h in history)
            # Orden existente del SP: timestamps nulos se ordenan al final en DESC.
            order = [(h["fecha_hora"] or "", h["id_historial"]) for h in history]
            assert order == sorted(order, reverse=True)
            kinds.add("history")
            demos = get(prefix + "/demos")
            assert all(d["id_vehiculo"] == identifier for d in demos)
            kinds.add("demos")
            if demos and demo_owner is None:
                demo_owner = (identifier, demos[0]["id_demo"])
        if demo_owner is None:
            for vehicle in vehicles[3:]:
                identifier = vehicle["id_vehiculo"]
                demos = get(f"/vehiculos/{identifier}/demos")
                if demos:
                    demo_owner = (identifier, demos[0]["id_demo"])
                    break
        if demo_owner:
            owner, demo = demo_owner
            rows = get(f"/vehiculos/{owner}/demos/{demo}/movimientos")
            assert [r["orden"] for r in rows] == sorted(r["orden"] for r in rows)
            kinds.add("demo_movements")
            other_vehicle = next((v["id_vehiculo"] for v in vehicles
                                  if v["id_vehiculo"] != owner), None)
            if other_vehicle is not None:
                get(f"/vehiculos/{other_vehicle}/demos/{demo}/movimientos", expected=404)
        else:
            report["warnings"].append(
                "No hay DEMO disponibles: su GET se verifica aisladamente, no con datos simulados en RDS."
            )
        existing_ids = {v["id_vehiculo"] for v in vehicles}
        missing = next(i for i in range(1, len(existing_ids) + 2) if i not in existing_ids)
        get(f"/vehiculos/{missing}", expected=404)
        get(f"/vehiculos/{missing}/historial", expected=404)
        if not vehicles:
            report["warnings"].append("Sin vehículos existentes; solo se pudo comprobar la lista y 404 reales.")
        report["status"] = "partial" if report["warnings"] else "ok"
    except (URLError, TimeoutError):
        report["warnings"].append("No fue posible completar las solicitudes al servidor local.")
    except (AssertionError, RuntimeError, ValueError, KeyError, TypeError) as error:
        # Nunca se publica una respuesta de RDS ni un traceback.
        report["warnings"].append(
            str(error) if isinstance(error, RuntimeError) else "Una respuesta no cumple el contrato HTTP."
        )
    report["functional_get_kinds_checked"] = len(kinds)
    try:
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print("No fue posible guardar la verificacion HTTP.")
        return 1
    print(f"Estado HTTP real: {report['status']}; tipos GET funcionales comprobados: {len(kinds)}/7.")
    print(f"Solicitudes GET: {len(report['requests'])}; escrituras: 0.")
    print("Evidencia: docs/verificacion_http_get.json (sin cuerpos de datos ni secretos).")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
