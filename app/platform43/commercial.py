"""Estado y transacciones comerciales de NODE/01."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.database.connection import MySQLConnectionManager
from app.errors import ApplicationError
from app.repositories.vehiculos import VehiculoRepository
from app.services.continuous import VEHICLE_ID as DEVICE_VEHICLE_ID


class CommercialSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IOT43_",
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    guest_seconds: int = Field(default=120, ge=30, le=180)


@lru_cache
def commercial_settings():
    return CommercialSettings()


class CommercialStore:
    """Todas las consultas utilizan SQL interno y valores enlazados."""

    def __init__(self, manager=None):
        self.manager = manager or MySQLConnectionManager()
        self.legacy = VehiculoRepository(self.manager)

    def rows(self, db, sql, params=()):
        with self.manager.cursor(db, dictionary=True) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def optional(self, db, sql, params=()):
        records = self.rows(db, sql, params)
        return records[0] if records else None

    def write(self, db, sql, params=()):
        with self.manager.cursor(db) as cursor:
            cursor.execute(sql, params)
            return cursor.lastrowid

    def unit(self, db, unit_id, lock=False):
        row = self.optional(db, """
            SELECT u.*,m.nombre AS modelo,m.activo AS modelo_activo,m.publicado AS modelo_publicado
            FROM iot43_unidades u JOIN iot43_modelos m ON m.id=u.id_modelo
            WHERE u.id=%s
        """ + (" FOR UPDATE OF u" if lock else ""), (unit_id,))
        if row is None:
            raise ApplicationError(404, "Unidad no encontrada.", "unit_missing")
        return row

    def capabilities(self, db, model_id):
        return self.rows(db, """
            SELECT codigo,control,invitado FROM iot43_capacidades WHERE id_modelo=%s ORDER BY codigo
        """, (model_id,))


store = CommercialStore()


def _missing(resource):
    raise ApplicationError(404, f"{resource} no encontrado.", "commercial_missing")


def _conflict(message):
    raise ApplicationError(409, message, "commercial_conflict")


def check_version(row, version):
    if version is None or row["version"] != version:
        _conflict("El registro cambió. Actualiza los datos y vuelve a intentarlo.")


def owner_unit(db, actor, unit_id, lock=False):
    unit = store.unit(db, unit_id, lock=lock)
    if actor.rol != "comprador" or unit["id_cliente"] != actor.id or unit["estado"] != "vendida":
        raise ApplicationError(404, "Unidad no encontrada.", "unit_missing")
    return unit


def eligible_reservation(db, actor, unit):
    if (actor.rol != "invitado" or unit["fisico"] or unit["estado"] != "exhibicion"
            or unit["id_vehiculo"] == DEVICE_VEHICLE_ID
            or unit["id_cliente"] is not None or not unit["modelo_activo"] or not unit["modelo_publicado"]):
        return False
    return store.optional(db, """
        SELECT id FROM iot43_reservas WHERE id=%s AND id_unidad=%s
           AND ended_at IS NULL AND expires_at>UTC_TIMESTAMP(6)
    """, (actor.reservation_id, unit["id"])) is not None


def unit_for_actor(db, actor, unit_id, lock=False):
    if actor.rol == "comprador":
        return owner_unit(db, actor, unit_id, lock=lock)
    unit = store.unit(db, unit_id, lock=lock)
    if not eligible_reservation(db, actor, unit):
        _missing("Vehículo")
    return unit


def dashboard(db):
    return store.optional(db, """
        SELECT
          (SELECT COUNT(*) FROM iot43_modelos WHERE activo=1) AS modelos_activos,
          (SELECT COUNT(*) FROM iot43_unidades WHERE estado<>'inactiva') AS unidades_activas,
          (SELECT COUNT(*) FROM iot43_unidades WHERE estado='inventario') AS unidades_inventario,
          (SELECT COUNT(*) FROM iot43_unidades WHERE estado='vendida') AS unidades_vendidas,
          (SELECT COUNT(*) FROM iot43_unidades WHERE estado='exhibicion') AS unidades_exhibicion,
          (SELECT COUNT(*) FROM iot43_cuentas WHERE rol='comprador' AND activo=1) AS clientes_activos,
          (SELECT COUNT(*) FROM iot43_demos WHERE activo=1) AS demos_activas,
          (SELECT COUNT(*) FROM iot43_reservas WHERE ended_at IS NULL AND expires_at>UTC_TIMESTAMP(6)) AS reservas_activas
    """)


def models(db, query=""):
    search = f"%{query}%"
    result = store.rows(db, """
        SELECT id,sku,nombre,descripcion,publicado,activo FROM iot43_modelos
        WHERE nombre LIKE %s OR sku LIKE %s ORDER BY id DESC LIMIT 200
    """, (search, search))
    for item in result:
        item["capacidades"] = store.capabilities(db, item["id"])
    return result


def model(db, model_id, lock=False):
    row = store.optional(db, """
        SELECT id,sku,nombre,descripcion,publicado,activo FROM iot43_modelos WHERE id=%s
    """ + (" FOR UPDATE" if lock else ""), (model_id,))
    if row is None:
        _missing("Modelo")
    row["capacidades"] = store.capabilities(db, model_id)
    return row


def _fence(db, unit):
    from app.platform43.auth import revoke_vehicle

    if unit["id_vehiculo"] is not None:
        revoke_vehicle(unit["id_vehiculo"])
    store.write(db, """
        UPDATE iot43_sesiones s JOIN iot43_reservas r ON r.id=s.id_reserva
        SET s.revoked_at=UTC_TIMESTAMP(6) WHERE r.id_unidad=%s AND s.revoked_at IS NULL
    """, (unit["id"],))
    store.write(db, """
        UPDATE iot43_reservas SET ended_at=UTC_TIMESTAMP(6)
        WHERE id_unidad=%s AND ended_at IS NULL
    """, (unit["id"],))


def save_model(db, payload, model_id=None):
    values = (payload.sku, payload.nombre, payload.descripcion, payload.publicado, payload.activo)
    if model_id is None:
        model_id = store.write(db, """
            INSERT INTO iot43_modelos (sku,nombre,descripcion,publicado,activo) VALUES (%s,%s,%s,%s,%s)
        """, values)
    else:
        model(db, model_id, lock=True)
        units = store.rows(db, "SELECT id,id_vehiculo FROM iot43_unidades WHERE id_modelo=%s ORDER BY id FOR UPDATE",
                           (model_id,))
        for unit in units:
            _fence(db, unit)
        store.write(db, "UPDATE iot43_unidades SET version=version+1 WHERE id_modelo=%s", (model_id,))
        store.write(db, """
            UPDATE iot43_modelos SET sku=%s,nombre=%s,descripcion=%s,publicado=%s,activo=%s WHERE id=%s
        """, (*values, model_id))
        store.write(db, "DELETE FROM iot43_capacidades WHERE id_modelo=%s", (model_id,))
    for cap in payload.capacidades:
        store.write(db, """
            INSERT INTO iot43_capacidades (id_modelo,codigo,control,invitado) VALUES (%s,%s,%s,%s)
        """, (model_id, cap.codigo, cap.control, cap.invitado))
    return model(db, model_id)


def units(db, query="", state=None):
    like = f"%{query}%"
    records = store.rows(db, """
        SELECT u.*,m.nombre AS modelo, m.activo AS modelo_activo
        FROM iot43_unidades u JOIN iot43_modelos m ON m.id=u.id_modelo
        WHERE (u.nombre LIKE %s OR u.serie LIKE %s OR m.nombre LIKE %s) AND (%s IS NULL OR u.estado=%s)
        ORDER BY u.id DESC LIMIT 200
    """, (like, like, like, state, state))
    from app.runtime import runtime
    for unit in records:
        if unit["id_vehiculo"] is None:
            unit["despacho"] = "Sin vincular"
        elif unit["fisico"]:
            diagnostics = runtime.continuous.capability(unit["id_vehiculo"])
            unit["despacho"] = "Latido reciente" if diagnostics.disponible else "Sin verificar"
        else:
            unit["despacho"] = "Virtual"
    return records


def save_unit(db, payload, unit_id=None):
    if payload.id_vehiculo == DEVICE_VEHICLE_ID and not payload.fisico:
        _conflict("El dispositivo físico conocido no puede registrarse como unidad virtual.")
    selected_model = model(db, payload.id_modelo, lock=True)
    if not selected_model["activo"] and payload.estado != "inactiva":
        _conflict("El modelo debe estar activo para habilitar esta unidad.")
    if unit_id is None:
        if payload.estado == "vendida":
            _conflict("Utiliza la asignación para vender una unidad.")
        if payload.id_vehiculo is not None:
            legacy = store.legacy.obtener_vehiculo(db, payload.id_vehiculo)
            if legacy is None:
                _missing("Vehículo IoT")
            if not legacy["activo"] and payload.estado != "inactiva":
                _conflict("El vehículo IoT vinculado está inactivo.")
        unit_id = store.write(db, """
            INSERT INTO iot43_unidades (id_vehiculo,id_modelo,serie,nombre,estado,fisico) VALUES (%s,%s,%s,%s,%s,%s)
        """, (payload.id_vehiculo, payload.id_modelo, payload.serie, payload.nombre, payload.estado, payload.fisico))
    else:
        unit = store.unit(db, unit_id, lock=True)
        check_version(unit, payload.version)
        if (unit["id_vehiculo"], bool(unit["fisico"])) != (payload.id_vehiculo, payload.fisico):
            _conflict("El vínculo IoT y el tipo de despacho se establecen al dar la unidad de alta.")
        if unit["id_cliente"] is not None and payload.estado not in ("vendida", "inactiva"):
            _conflict("Retira primero la asignación de esta unidad.")
        if unit["id_cliente"] is None and payload.estado == "vendida":
            _conflict("Utiliza la asignación para vender una unidad.")
        if unit["id_cliente"] is not None and unit["id_modelo"] != payload.id_modelo:
            _conflict("Retira la asignación antes de cambiar el modelo.")
        _fence(db, unit)
        store.write(db, """
            UPDATE iot43_unidades SET id_modelo=%s,serie=%s,nombre=%s,estado=%s,version=version+1 WHERE id=%s
        """, (payload.id_modelo, payload.serie, payload.nombre, payload.estado, unit_id))
    return store.unit(db, unit_id)


def clients(db, query=""):
    search = f"%{query}%"
    return store.rows(db, """
        SELECT id,correo,nombre,activo FROM iot43_cuentas
        WHERE rol='comprador' AND (nombre LIKE %s OR correo LIKE %s) ORDER BY id DESC LIMIT 200
    """, (search, search))


def client(db, client_id, lock=False):
    row = store.optional(db, """
        SELECT id,correo,nombre,activo FROM iot43_cuentas WHERE id=%s AND rol='comprador'
    """ + (" FOR UPDATE" if lock else ""), (client_id,))
    if row is None:
        _missing("Cliente")
    return row


def add_client(db, payload, password_hash):
    account = store.write(db, """
        INSERT INTO iot43_cuentas (correo,nombre,hash_contrasena,rol) VALUES (%s,%s,%s,'comprador')
    """, (payload.correo.casefold(), payload.nombre, password_hash))
    return client(db, account)


def set_client_status(db, client_id, active):
    client(db, client_id, lock=True)
    store.write(db, "UPDATE iot43_cuentas SET activo=%s WHERE id=%s", (active, client_id))
    if not active:
        for unit in store.rows(db, "SELECT id,id_vehiculo FROM iot43_unidades WHERE id_cliente=%s ORDER BY id FOR UPDATE",
                               (client_id,)):
            _fence(db, unit)
        store.write(db, "UPDATE iot43_unidades SET version=version+1 WHERE id_cliente=%s", (client_id,))
        store.write(db, "UPDATE iot43_sesiones SET revoked_at=UTC_TIMESTAMP(6) WHERE id_cuenta=%s AND revoked_at IS NULL",
                    (client_id,))
    return client(db, client_id)


def assign(db, actor, unit_id, payload):
    if payload.id_cliente is not None and not client(db, payload.id_cliente, lock=True)["activo"]:
        _conflict("El cliente está bloqueado.")
    unit = store.unit(db, unit_id, lock=True)
    check_version(unit, payload.version)
    if unit["id_cliente"] == payload.id_cliente:
        return unit
    if payload.id_cliente is not None and (unit["estado"] == "inactiva" or not unit["modelo_activo"]):
        _conflict("Una unidad o modelo inactivo no puede asignarse.")
    _fence(db, unit)
    store.write(db, "UPDATE iot43_titularidad SET fin=UTC_TIMESTAMP(6) WHERE id_unidad=%s AND fin IS NULL",
                (unit_id,))
    active_title = None
    state = "inactiva" if unit["estado"] == "inactiva" else "inventario"
    if payload.id_cliente is not None:
        active_title = store.write(db, """
            INSERT INTO iot43_titularidad (id_unidad,id_cliente,id_actor) VALUES (%s,%s,%s)
        """, (unit_id, payload.id_cliente, actor.id))
        state = "vendida"
    store.write(db, """
        UPDATE iot43_unidades SET id_cliente=%s,id_titularidad=%s,estado=%s,version=version+1 WHERE id=%s
    """, (payload.id_cliente, active_title, state, unit_id))
    return store.unit(db, unit_id)


def title_history(db, unit_id):
    store.unit(db, unit_id)
    records = store.rows(db, """
        SELECT t.id,t.id_cliente,c.nombre AS cliente,t.id_actor,t.inicio,t.fin
        FROM iot43_titularidad t JOIN iot43_cuentas c ON c.id=t.id_cliente
        WHERE t.id_unidad=%s ORDER BY t.id DESC LIMIT 200
    """, (unit_id,))
    return _utc_fields(records, "inicio", "fin")


def my_units(db, actor):
    return store.rows(db, """
        SELECT u.id,u.id_vehiculo,u.id_modelo,u.nombre,u.serie,u.estado,u.fisico,u.version,m.nombre AS modelo
        FROM iot43_unidades u JOIN iot43_modelos m ON m.id=u.id_modelo
        WHERE u.id_cliente=%s AND u.estado='vendida' ORDER BY u.id
    """, (actor.id,))


def public_catalog(db):
    result = store.rows(db, """
        SELECT id,sku,nombre,descripcion FROM iot43_modelos WHERE activo=1 AND publicado=1
        ORDER BY nombre LIMIT 200
    """)
    for item in result:
        item["capacidades"] = [{"codigo": cap["codigo"], "control": bool(cap["control"]),
                                "invitado": bool(cap["invitado"])}
                               for cap in store.capabilities(db, item["id"]) if cap["control"]]
        allowed = any(cap["invitado"] for cap in item["capacidades"])
        item["unidades"] = store.rows(db, """
            SELECT u.id,u.nombre,
              (COALESCE(v.activo,0)=1 AND u.id_vehiculo<>%s AND %s AND NOT EXISTS (
                SELECT 1 FROM iot43_reservas r WHERE r.id_unidad=u.id
                AND r.ended_at IS NULL AND r.expires_at>UTC_TIMESTAMP(6)
              )) AS disponible
            FROM iot43_unidades u LEFT JOIN vehiculos v ON v.id_vehiculo=u.id_vehiculo
            WHERE u.id_modelo=%s AND u.estado='exhibicion' AND u.fisico=0 AND u.id_cliente IS NULL
            ORDER BY u.id
        """, (DEVICE_VEHICLE_ID, allowed, item["id"]))
        for unit in item["unidades"]:
            unit["disponible"] = bool(unit["disponible"])
    return result


def reserve(db, unit_id):
    from app.platform43.auth import issue_session

    unit = store.unit(db, unit_id, lock=True)
    if (unit["estado"] != "exhibicion" or unit["fisico"] or unit["id_cliente"] is not None
            or unit["id_vehiculo"] in (None, DEVICE_VEHICLE_ID) or not unit["modelo_activo"] or not unit["modelo_publicado"]
            or not any(c["invitado"] for c in store.capabilities(db, unit["id_modelo"]))):
        _missing("Unidad de exhibición")
    vehicle = store.legacy.obtener_vehiculo(db, unit["id_vehiculo"])
    if vehicle is None or not vehicle["activo"]:
        _missing("Unidad de exhibición")
    expired = store.optional(db, """
        SELECT id,expires_at<=UTC_TIMESTAMP(6) AS vencida FROM iot43_reservas
        WHERE id_unidad=%s AND ended_at IS NULL FOR UPDATE
    """, (unit_id,))
    if expired is not None:
        if not expired["vencida"]:
            _conflict("La unidad tiene una reserva vigente.")
        _fence(db, unit)
    expires = datetime.now(timezone.utc) + timedelta(seconds=commercial_settings().guest_seconds)
    reservation = store.write(db, "INSERT INTO iot43_reservas (id_unidad,expires_at) VALUES (%s,%s)",
                              (unit_id, expires.replace(tzinfo=None)))
    token, expiry = issue_session(db, id_reserva=reservation, expires_at=expires)
    return {"token": token, "expires_at": expiry, "id_reserva": reservation,
            "unidad": {"fisico": False, **{k: unit[k] for k in ("id", "id_vehiculo", "id_modelo", "nombre", "modelo", "version")}}}


def reservations(db):
    rows = store.rows(db, """
        SELECT r.id,r.id_unidad,u.nombre AS unidad,r.created_at,r.expires_at,r.ended_at,
          (r.ended_at IS NULL AND r.expires_at>UTC_TIMESTAMP(6)) AS activa
        FROM iot43_reservas r JOIN iot43_unidades u ON u.id=r.id_unidad ORDER BY r.id DESC LIMIT 200
    """)
    return _utc_fields(rows, "created_at", "expires_at", "ended_at")


def revoke_reservation(db, reservation_id):
    row = store.optional(db, "SELECT id,id_unidad FROM iot43_reservas WHERE id=%s", (reservation_id,))
    if row is None:
        _missing("Reserva")
    unit = store.unit(db, row["id_unidad"], lock=True)
    current = store.optional(db, "SELECT ended_at FROM iot43_reservas WHERE id=%s FOR UPDATE", (reservation_id,))
    if not current["ended_at"]:
        _fence(db, unit)
        store.write(db, "UPDATE iot43_unidades SET version=version+1 WHERE id=%s", (unit["id"],))
    return {"id": reservation_id, "revocada": True}


def audit_records(db):
    rows = store.rows(db, """
        SELECT a.id, a.id_actor, c.nombre AS actor, a.accion,a.recurso,a.id_recurso,a.resultado,a.fecha
        FROM iot43_auditoria a LEFT JOIN iot43_cuentas c ON c.id=a.id_actor
        ORDER BY a.id DESC LIMIT 200
    """)
    return _utc_fields(rows, "fecha")


def _utc_fields(rows, *names):
    for item in rows:
        for name in names:
            if item[name] is not None:
                item[name] = item[name].replace(tzinfo=timezone.utc)
    return rows


def _steps(db, demo_id):
    return store.rows(db, """
        SELECT orden,codigo,duracion_ms,pausa_ms FROM iot43_pasos WHERE id_demo=%s ORDER BY orden
    """, (demo_id,))


def _demo(db, demo_id, lock=False):
    row = store.optional(db, """
        SELECT id,id_modelo,id_unidad,id_titularidad,nombre,descripcion,activo,version
        FROM iot43_demos WHERE id=%s
    """ + (" FOR UPDATE" if lock else ""), (demo_id,))
    if row is None:
        _missing("DEMO")
    return row


def _demo_projection(row, steps):
    return {**{k: row[k] for k in ("id", "id_unidad", "nombre", "descripcion", "version")},
            "id_modelo": row["id_modelo"] if row["id_unidad"] is None else None,
            "tipo": "fabrica" if row["id_unidad"] is None else "privada",
            "activo": bool(row["activo"]), "pasos": steps,
            "total_ms": sum(step["duracion_ms"] + step["pausa_ms"] for step in steps)}


def _permitted_codes(db, model_id, *, guest=False):
    return {item["codigo"] for item in store.capabilities(db, model_id)
            if item["control"] and (not guest or item["invitado"])}


def list_demos(db, actor, model_id=None, unit_id=None):
    if actor.rol == "administrador":
        if model_id is None or unit_id is not None:
            raise ApplicationError(422, "Selecciona un modelo de fábrica.", "demo_scope")
        selected = model(db, model_id)
        records = store.rows(db, """
            SELECT id,id_modelo,id_unidad,nombre,descripcion,activo,version
            FROM iot43_demos WHERE id_modelo=%s AND id_unidad IS NULL ORDER BY id DESC LIMIT 100
        """, (selected["id"],))
        return [_demo_projection(row, _steps(db, row["id"])) for row in records]
    if unit_id is None or model_id is not None:
        raise ApplicationError(422, "Selecciona una unidad.", "demo_scope")
    unit = unit_for_actor(db, actor, unit_id)
    records = store.rows(db, """
        SELECT id,id_modelo,id_unidad,id_titularidad,nombre,descripcion,activo,version
        FROM iot43_demos WHERE id_modelo=%s AND ((id_unidad IS NULL AND activo=1)
            OR (id_unidad=%s AND id_titularidad=%s)) ORDER BY id DESC LIMIT 100
    """, (unit["id_modelo"], unit_id, unit["id_titularidad"]))
    result = []
    codes = _permitted_codes(db, unit["id_modelo"], guest=actor.rol == "invitado")
    if unit["id_vehiculo"] is not None:
        codes &= {command["codigo"] for command in store.legacy.obtener_comandos(db, unit["id_vehiculo"])
                  if command["activo"]}
    for row in records:
        steps = _steps(db, row["id"])
        if row["id_unidad"] is not None or all(step["codigo"] in codes for step in steps):
            result.append(_demo_projection(row, steps))
    return result


def _edit_scope(db, actor, model_id, unit_id):
    if actor.rol == "administrador" and model_id is not None and unit_id is None:
        selected = model(db, model_id, lock=True)
        return selected["id"], None, None, None
    if actor.rol == "comprador" and unit_id is not None and model_id is None:
        unit = owner_unit(db, actor, unit_id, lock=True)
        return unit["id_modelo"], unit_id, unit["id_titularidad"], unit["id_vehiculo"]
    raise ApplicationError(403, "La cuenta no puede editar esta DEMO.", "demo_editor_forbidden")


def _editable(row, context):
    if (row["id_modelo"], row["id_unidad"], row["id_titularidad"]) != context[:3]:
        _missing("DEMO")


def save_demo(db, actor, payload, demo_id=None):
    scope = _edit_scope(db, actor, payload.id_modelo, payload.id_unidad)
    codes = _permitted_codes(db, scope[0])
    if scope[3] is not None:
        commands = store.legacy.obtener_comandos(db, scope[3])
        codes &= {command["codigo"] for command in commands if command["activo"]}
    if any(step.codigo not in codes for step in payload.pasos):
        _conflict("La DEMO contiene un comando inactivo o incompatible.")
    if demo_id is None:
        demo_id = store.write(db, """
            INSERT INTO iot43_demos (id_modelo,id_unidad,id_titularidad,nombre,descripcion,activo)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (*scope[:3], payload.nombre, payload.descripcion, payload.activo))
    else:
        existing = _demo(db, demo_id, lock=True)
        _editable(existing, scope)
        check_version(existing, payload.version)
        store.write(db, """
            UPDATE iot43_demos SET nombre=%s,descripcion=%s,activo=%s,version=version+1 WHERE id=%s
        """, (payload.nombre, payload.descripcion, payload.activo, demo_id))
        store.write(db, "DELETE FROM iot43_pasos WHERE id_demo=%s", (demo_id,))
    for order, step in enumerate(payload.pasos, 1):
        store.write(db, """
            INSERT INTO iot43_pasos (id_demo,orden,codigo,duracion_ms,pausa_ms) VALUES (%s,%s,%s,%s,%s)
        """, (demo_id, order, step.codigo, step.duracion_ms, step.pausa_ms))
    return _demo_projection(_demo(db, demo_id), _steps(db, demo_id))


def disable_demo(db, actor, demo_id):
    row = _demo(db, demo_id)
    scope = _edit_scope(db, actor, row["id_modelo"] if row["id_unidad"] is None else None, row["id_unidad"])
    _editable(_demo(db, demo_id, lock=True), scope)
    store.write(db, "UPDATE iot43_demos SET activo=0,version=version+1 WHERE id=%s AND activo=1", (demo_id,))


def get_demo_plan(db, actor, demo_id, unit_id):
    """Devuelve un plan aprobado para la titularidad o reserva vigente."""
    unit = unit_for_actor(db, actor, unit_id, lock=True)
    row = _demo(db, demo_id)
    if (row["id_modelo"] != unit["id_modelo"] or not row["activo"] or not unit["modelo_activo"]
            or unit["id_vehiculo"] is None):
        _missing("DEMO")
    if row["id_unidad"] is not None and (
        row["id_unidad"] != unit_id or row["id_titularidad"] != unit["id_titularidad"] or actor.rol != "comprador"
    ):
        _missing("DEMO")
    codes = _permitted_codes(db, unit["id_modelo"], guest=actor.rol == "invitado")
    commands = {command["codigo"]: command for command in store.legacy.obtener_comandos(db, unit["id_vehiculo"])
                if command["activo"] and command["codigo"] in codes}
    steps = _steps(db, demo_id)
    if not steps or any(step["codigo"] not in commands for step in steps):
        _conflict("La DEMO tiene pasos que esta unidad no puede ejecutar.")
    return {"id": demo_id, "id_unidad": unit_id, "id_vehiculo": unit["id_vehiculo"], "nombre": row["nombre"],
            "version_unidad": unit["version"], "fisico": bool(unit["fisico"]),
            "pasos": [{**step, "id_comando": commands[step["codigo"]]["id_comando"],
                       "comando": commands[step["codigo"]]["nombre"]} for step in steps]}
