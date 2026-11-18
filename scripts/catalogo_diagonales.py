"""Audita o administra las cuatro diagonales del vehículo 1 mediante la API."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from unicodedata import combining, normalize
from urllib.parse import urlsplit
import uuid
import warnings

from scripts.verification_common import HttpClient, Target, VerificationFailure


VEHICLE = 1
PATH = f"/vehiculos/{VEHICLE}/comandos"
DIAGONALS = {
    9: "AVANZAR_IZQUIERDA",
    10: "AVANZAR_DERECHA",
    11: "RETROCEDER_IZQUIERDA",
    12: "RETROCEDER_DERECHA",
}
REMOTE_ERROR = (OSError, ValueError, VerificationFailure)


class CatalogError(RuntimeError):
    """Mensajes propios que nunca incluyen secretos ni contenido remoto."""


@dataclass(frozen=True)
class Command:
    identifier: int
    name: str
    code: int
    description: str | None
    active: bool


def parse_catalog(payload):
    if not isinstance(payload, list):
        raise CatalogError("La lectura del catálogo no devolvió una lista.")
    catalog = {}
    for row in payload:
        if not isinstance(row, dict):
            raise CatalogError("El catálogo incluye una fila inválida.")
        identifier, vehicle, name, code, description, active = (
            row.get(key) for key in ("id_comando", "id_vehiculo", "nombre", "codigo", "descripcion", "activo")
        )
        if (type(vehicle) is not int or vehicle != VEHICLE or type(identifier) is not int
                or not 1 <= identifier <= 2_147_483_647 or identifier in catalog
                or not isinstance(name, str) or not name or type(code) is not int
                or (description is not None and not isinstance(description, str))
                or type(active) is not bool or "descripcion" not in row):
            raise CatalogError("El catálogo contiene una fila inválida, ajena o con ID duplicado.")
        catalog[identifier] = Command(identifier, name, code, description, active)
    return catalog


def read_catalog(http):
    try:
        return parse_catalog(http.get(PATH))
    except REMOTE_ERROR:
        raise CatalogError("No se pudo consultar el catálogo del vehículo 1.") from None


def folded_name(value):
    return "".join(char for char in normalize("NFKD", value.strip().casefold()) if not combining(char))


def plan(catalog):
    """Detecta colisiones antes de modificar el catálogo."""
    expected_names = {folded_name(name): code for code, name in DIAGONALS.items()}
    exact = {}
    conflicts = {code: set() for code in DIAGONALS}
    for row in catalog.values():
        code_by_name = expected_names.get(folded_name(row.name))
        related_codes = {row.code, code_by_name}.intersection(DIAGONALS)
        for code in related_codes:
            if row.name == DIAGONALS[code] and row.code == code and code not in exact:
                exact[code] = row
            else:
                conflicts[code].add(row.identifier)
    for code, identifiers in conflicts.items():
        if identifiers:
            raise CatalogError(
                f"Colisión para {DIAGONALS[code]} código {code}. IDs observados: "
                + ", ".join(map(str, sorted(identifiers)))
            )
    return exact


def show_plan(current):
    for code, name in DIAGONALS.items():
        row = current.get(code)
        status = "faltante"
        if row is not None:
            status = f"ID {row.identifier}, " + ("activo" if row.active else "inactivo")
        print(f"{code} {name} · {status}")


@contextmanager
def reserve_journal(path):
    """Excluye cambios simultáneos con el mismo archivo local."""
    lock = path.with_name(path.name + ".lock")
    with lock.open("a+b") as handle:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CatalogError("El diario está en uso por otra operación local.") from None
        yield


class Journal:
    def __init__(self, path, base, *, restore=False):
        self.path = path
        self.base = base
        self.batch = uuid.uuid4().hex
        self.rows = {}
        if path.exists():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                batch = document["batch"]
                rows = document["rows"]
                if (document["version"] != 1 or document["base_url"] != base
                        or document["vehicle"] != VEHICLE or uuid.UUID(batch).hex != batch
                        or not isinstance(rows, dict)):
                    raise ValueError
                seen = set()
                for code, identifier in rows.items():
                    if (code not in {str(value) for value in DIAGONALS}
                            or (identifier is not None and
                                (type(identifier) is not int or not 1 <= identifier <= 2_147_483_647
                                 or identifier in seen))):
                        raise ValueError
                    self.rows[int(code)] = identifier
                    if identifier is not None:
                        seen.add(identifier)
                self.batch = batch
            except (OSError, TypeError, ValueError, KeyError, AttributeError):
                raise CatalogError("El diario es inválido o corresponde a otro destino.") from None
        elif restore:
            raise CatalogError("Para restaurar se requiere el diario del alta.")

    def description(self, code):
        text = DIAGONALS[code].replace("_", " ").capitalize()
        return f"{text} · NODE/01 · {self.batch}"

    def record(self, code, identifier):
        self.rows[code] = identifier
        data = {"version": 1, "base_url": self.base, "vehicle": VEHICLE,
                "batch": self.batch, "rows": self.rows}
        temporary = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                    prefix=".diagonales-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(data, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def request_admin_key():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass("Clave administrativa privada: ")
        except (EOFError, getpass.GetPassWarning):
            raise CatalogError("Abre una terminal que admita introducir la clave sin eco.") from None
    if not value or any(not 33 <= ord(char) <= 126 for char in value):
        raise CatalogError("La clave administrativa no es válida para la cabecera HTTP.")
    return value


def post(http, path, body):
    if http.key is None:
        http.key = request_admin_key()
    try:
        status, _ = http.request("POST", path, body, expect=None)
        return status
    except REMOTE_ERROR:
        # Puede haberse confirmado. La lectura posterior resuelve el resultado.
        return None


def ensure_owned(row, code, journal):
    expected_id = journal.rows[code]
    if row is None:
        raise CatalogError(f"No se confirma el alta del código {code}. Conserva el diario y vuelve a consultar.")
    if (row.code != code or row.name != DIAGONALS[code] or row.description != journal.description(code)
            or (expected_id is not None and row.identifier != expected_id)):
        raise CatalogError(f"El registro del código {code} cambió o pertenece a otra operación.")
    return row


def apply(http, journal, current):
    for code, identifier in journal.rows.items():
        row = current.get(code)
        if row is not None or identifier is not None:
            ensure_owned(row, code, journal)
    for code, name in DIAGONALS.items():
        row = current.get(code)
        if row is not None:
            if code in journal.rows and journal.rows[code] is None:
                journal.record(code, row.identifier)
            continue

        if code not in journal.rows:
            journal.record(code, None)
        description = journal.description(code)
        status = post(http, PATH, {"nombre": name, "codigo": code, "descripcion": description})
        current = plan(read_catalog(http))
        row = current.get(code)
        if row is None and status is not None:
            print(f"El alta del código {code} devolvió HTTP {status}.")
        owned = ensure_owned(row, code, journal)
        if not owned.active:
            raise CatalogError(f"El alta del código {code} no está activa. Conserva el diario.")
        journal.record(code, owned.identifier)
        print(f"Alta confirmada · {name} · ID {owned.identifier}")
    show_plan(current)


def restore(http, journal):
    catalog = read_catalog(http)
    current = plan(catalog)
    candidates = []
    unresolved = []
    for code, identifier in sorted(journal.rows.items()):
        row = current.get(code)
        if row is None and identifier is None:
            unresolved.append(code)
        else:
            candidates.append(ensure_owned(row, code, journal))
    for candidate in candidates:
        row = ensure_owned(read_catalog(http).get(candidate.identifier), candidate.code, journal)
        if journal.rows[row.code] is None:
            journal.record(row.code, row.identifier)
        if row.active:
            payload = {"nombre": row.name, "codigo": row.code, "descripcion": row.description, "activo": False}
            post(http, f"{PATH}/{row.identifier}/actualizar", payload)
            row = ensure_owned(read_catalog(http).get(row.identifier), row.code, journal)
            if row.active:
                raise CatalogError(f"No se confirmó la desactivación del ID {row.identifier}. Conserva el diario.")
        print(f"Restauración verificada · {row.name} · ID {row.identifier} inactivo")
    if unresolved:
        raise CatalogError("Altas sin confirmar para los códigos " + ", ".join(map(str, unresolved))
                           + ". Conserva el diario y repite la comprobación.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Raíz de la API HTTPS, o HTTP en loopback.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Crea las diagonales faltantes.")
    mode.add_argument("--restore", action="store_true", help="Desactiva las altas reconocidas en el diario.")
    parser.add_argument("--journal", type=Path, help="Diario local del alta. Requerido con --apply o --restore.")
    args = parser.parse_args(argv)
    if (args.apply or args.restore) and args.journal is None:
        parser.error("--apply y --restore requieren --journal.")
    try:
        target = Target.create(args.base_url)
        parsed = urlsplit(target.base)
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError
    except ValueError:
        parser.error("Usa HTTPS o HTTP de loopback. La URL debe estar libre de credenciales.")
    http = HttpClient(target, timeout=15)
    try:
        if not (args.apply or args.restore):
            show_plan(plan(read_catalog(http)))
        else:
            path = args.journal.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with reserve_journal(path):
                journal = Journal(path, target.base, restore=args.restore)
                if args.apply:
                    apply(http, journal, plan(read_catalog(http)))
                else:
                    restore(http, journal)
    except CatalogError as error:
        print(error)
        return 1
    except (OSError, RuntimeError):
        print("No se pudo completar el diario local. Resuelve el acceso al archivo y vuelve a consultar.")
        return 1
    finally:
        http.key = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
