"""Genera claves API faltantes sin mostrar secretos ni reescribir el .env."""

from collections.abc import Mapping
from io import StringIO
import os
from pathlib import Path
import secrets

from dotenv import dotenv_values


API_KEY_NAMES = ("API_KEY_OPERATOR", "API_KEY_ADMIN")
PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"


def ensure_api_keys(
    path: str | Path = PROJECT_ENV,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Añade sólo claves vacías/ausentes; retorna sus nombres, nunca sus valores.

    Una variable de entorno no vacía tiene prioridad y no se copia al archivo.
    Los bytes existentes se conservan, incluso cuando una definición vacía
    necesita una nueva definición al final del archivo.
    """

    environment = os.environ if environ is None else environ
    env_path = Path(path)
    original = env_path.read_bytes() if env_path.exists() else b""
    contents = original.decode("utf-8-sig")
    file_values = dotenv_values(stream=StringIO(contents), interpolate=False)
    occupied = {
        environment.get(name) or file_values.get(name)
        for name in API_KEY_NAMES
        if environment.get(name) or file_values.get(name)
    }
    additions = {}
    for name in API_KEY_NAMES:
        if environment.get(name) or file_values.get(name):
            continue
        candidate = secrets.token_urlsafe(32)
        while candidate in occupied:
            candidate = secrets.token_urlsafe(32)
        additions[name] = candidate
        occupied.add(candidate)
    if not additions:
        return []

    newline = b"\r\n" if b"\r\n" in original else b"\n"
    prefix = newline if original and not original.endswith((b"\r", b"\n")) else b""
    lines = ["# Claves API locales: no versionar ni compartir."]
    lines.extend(f"{name}={value}" for name, value in additions.items())
    payload = prefix + newline.join(line.encode("utf-8") for line in lines) + newline
    with env_path.open("ab") as env_file:
        env_file.write(payload)
    return list(additions)


def main() -> int:
    """Informa sólo nombres y diagnósticos seguros sobre la configuración."""

    try:
        names = ensure_api_keys()
    except (OSError, UnicodeError):
        print("No fue posible preparar las claves API en el archivo .env local.")
        return 1

    if names:
        print("Claves API preparadas en .env: " + ", ".join(names) + ".")
    else:
        print("Las claves API existentes se conservaron.")

    empty_environment = [
        name for name in API_KEY_NAMES if name in os.environ and not os.environ[name]
    ]
    if empty_environment:
        print(
            "Hay variables vacías en el entorno que ocultan .env. Elimínalas "
            "de la sesión PowerShell antes de iniciar el servidor: "
            + ", ".join(empty_environment)
            + "."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
