"""Transporte y evidencia de verificadores; importar este modulo no abre red ni RDS."""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.verify_demo_real import EvidenceWriter


class VerificationFailure(RuntimeError):
    """Codigo interno fijo: nunca incluye respuestas, claves ni excepciones privadas."""


def require(condition, code):
    if not condition:
        raise VerificationFailure(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def serializable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def positive(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("El identificador debe ser un entero positivo.") from None
    if not 1 <= number <= 2147483647:
        raise argparse.ArgumentTypeError("El identificador debe estar entre 1 y 2147483647.")
    return number


def normalize_url(value, schemes):
    if any(c.isspace() for c in value) or '\\' in value:
        raise ValueError("URL invalida: no se permiten espacios ni barras inversas.")
    parts = urlsplit(value)
    if (parts.scheme not in schemes or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("URL invalida: usa un esquema permitido, sin credenciales, query ni fragmento.")
    try:
        parts.port
    except ValueError:
        raise ValueError("Puerto invalido.") from None
    if any(x in parts.path.split('/') for x in ('.', '..')):
        raise ValueError("Ruta base invalida.")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/'), '', ''))


@dataclass(frozen=True)
class Target:
    base: str
    ws_base: str

    @classmethod
    def create(cls, base, ws_base=None):
        base = normalize_url(base, ('http', 'https'))
        parsed = urlsplit(base)
        if ws_base is None:
            ws_base = urlunsplit(('wss' if parsed.scheme == 'https' else 'ws',
                                  parsed.netloc, parsed.path, '', ''))
        ws_base = normalize_url(ws_base, ('ws', 'wss'))
        ws = urlsplit(ws_base)
        if parsed.netloc != ws.netloc:
            raise ValueError("HTTP y WebSocket deben usar la misma autoridad del destino autorizado.")
        if (parsed.scheme == 'https') != (ws.scheme == 'wss'):
            raise ValueError("HTTPS requiere WSS y HTTP requiere WS.")
        return cls(base, ws_base)

    def http(self, path):
        require(path.startswith('/') and not path.startswith('//'), 'invalid_relative_path')
        return self.base + path

    def ws(self, vehicle_id):
        return self.ws_base + f'/ws/vehiculos/{positive(vehicle_id)}'


class NoRedirect(HTTPRedirectHandler):
    """No reenvia X-API-Key a otra URL ni convierte un POST en un GET."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpClient:
    def __init__(self, target, key=None, timeout=60):
        self.target, self.key, self.timeout = target, key, timeout
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None, *, expect=200, as_json=True):
        headers = {'Accept': 'application/json'}
        data = None
        if method == 'POST':
            require(bool(self.key), 'private_key_unavailable')
            headers['X-API-Key'] = self.key
            data = b'' if body is None else json.dumps(body).encode()
            if body is not None:
                headers['Content-Type'] = 'application/json'
        request = Request(self.target.http(path), data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except HTTPError as error:
            # No almacenar ni reflejar el cuerpo de errores/proxies no confiables.
            status, raw = error.code, b''
        if expect is not None:
            require(status == expect, f'http_{method.lower()}_{status}_expected_{expect}')
        if not raw:
            return status, None
        return status, json.loads(raw) if as_json else raw.decode('utf-8')

    def get(self, path):
        return self.request('GET', path)[1]

    async def aget(self, path):
        return await asyncio.to_thread(self.get, path)


def add_target_arguments(parser):
    parser.add_argument('--base-url', required=True, help='Raiz HTTP/HTTPS exacta; sin credenciales en la URL.')
    parser.add_argument('--ws-base-url', help='Raiz WS/WSS; por defecto se deriva de --base-url.')
    parser.add_argument('--vehicle-id', type=positive, help='ID real; si se omite en lecturas se elige uno existente.')
    parser.add_argument('--output', required=True, type=Path, help='Archivo NUEVO; se rechaza sobrescribir evidencia.')


def output_writer(path):
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return EvidenceWriter(path)


def history_is_ordered(rows, vehicle_id):
    keys = [(r.get('fecha_hora') or '', r['id_historial']) for r in rows]
    return (len(rows) <= 10 and all(r['id_vehiculo'] == vehicle_id for r in rows)
            and keys == sorted(keys, reverse=True))


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc))


def initial_matches(payload, state, vehicle_id):
    if payload.get('tipo') != 'estado_inicial' or payload.get('id_vehiculo') != vehicle_id:
        return False
    if datetime.fromisoformat(payload['fecha_hora'].replace('Z', '+00:00')).tzinfo is None:
        return False
    fields = ('id_comando', 'comando', 'codigo', 'conectado')
    if state is None:
        return all(payload.get(f) is None for f in fields)
    return (all(payload.get(f) == state[f] for f in fields)
            and (not state.get('fecha_actualizacion')
                 or timestamp(payload['fecha_hora']) == timestamp(state['fecha_actualizacion'])))
