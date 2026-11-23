"""Alta inicial privada. El proceso pide la contraseña sin eco."""

from getpass import getpass
import re
import secrets

from app.database.connection import DatabaseUnavailableError
from app.platform43 import auth


def main():
    email = input("Correo del administrador: ").strip().lower()
    name = input("Nombre del administrador: ").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254 or not 1 <= len(name) <= 100:
        print("El correo o el nombre no es válido.")
        return 1
    password = getpass("Contraseña de al menos 12 caracteres: ")
    confirmation = getpass("Confirma la contraseña: ")
    if not 12 <= len(password) <= 128 or not secrets.compare_digest(password.encode(), confirmation.encode()):
        print("Las contraseñas deben coincidir y tener de 12 a 128 caracteres.")
        return 1
    try:
        with auth.manager.transaction() as connection:
            existing = auth.one(connection, "SELECT id FROM iot43_cuentas WHERE correo=%s", (email,))
            if existing:
                print("El correo ya está registrado.")
                return 1
            account_id = auth.execute(connection, """INSERT INTO iot43_cuentas
                (correo, nombre, hash_contrasena, rol) VALUES (%s,%s,%s,'administrador')""",
                (email, name, auth.hash_password(password)))
            auth.audit(connection, account_id, "alta_admin", "cuenta", account_id)
    except DatabaseUnavailableError:
        print("No se pudo registrar al administrador. Revisa la migración y la conexión.")
        return 1
    print("Administrador registrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
