"""Acceso a los 21 SP del contrato; el servicio controla la transacción.

Los nombres SQL son constantes internas. Ningún cliente puede elegir un nombre
de procedimiento, tabla o columna y los valores siempre viajan enlazados.
"""

from enum import Enum
from typing import Any

from app.database.connection import DatabaseUnavailableError, MySQLConnectionManager


class Procedure(Enum):
    """Lista permitida, contrastada con docs/contrato_rds.json."""

    LISTAR_VEHICULOS = "sp_obtener_vehiculos"
    OBTENER_VEHICULO = "sp_obtener_vehiculo"
    OBTENER_COMANDOS = "sp_obtener_comandos_vehiculo"
    OBTENER_ESTADO = "sp_obtener_estado"
    OBTENER_HISTORIAL = "sp_obtener_historial"
    OBTENER_DEMOS = "sp_obtener_demos"
    OBTENER_MOVIMIENTOS_DEMO = "sp_obtener_movimientos_demo"
    CREAR_VEHICULO = "sp_crear_vehiculo"
    ACTUALIZAR_VEHICULO = "sp_actualizar_vehiculo"
    ELIMINAR_VEHICULO = "sp_eliminar_vehiculo"
    CREAR_COMANDO = "sp_crear_comando"
    ACTUALIZAR_COMANDO = "sp_actualizar_comando"
    ELIMINAR_COMANDO = "sp_eliminar_comando"
    CREAR_DEMO = "sp_crear_demo"
    ACTUALIZAR_DEMO = "sp_actualizar_demo"
    ELIMINAR_DEMO = "sp_eliminar_demo"
    CREAR_MOVIMIENTO_DEMO = "sp_agregar_movimiento_demo"
    ACTUALIZAR_MOVIMIENTO_DEMO = "sp_actualizar_movimiento_demo"
    ELIMINAR_MOVIMIENTO_DEMO = "sp_eliminar_movimiento_demo"
    ACTUALIZAR_ESTADO = "sp_actualizar_estado"
    REGISTRAR_HISTORIAL = "sp_registrar_historial"


_PARAMETER_COUNTS = {
    Procedure.LISTAR_VEHICULOS: 0,
    Procedure.OBTENER_VEHICULO: 1,
    Procedure.OBTENER_COMANDOS: 1,
    Procedure.OBTENER_ESTADO: 1,
    Procedure.OBTENER_HISTORIAL: 1,
    Procedure.OBTENER_DEMOS: 1,
    Procedure.OBTENER_MOVIMIENTOS_DEMO: 1,
    Procedure.CREAR_VEHICULO: 4,
    Procedure.ACTUALIZAR_VEHICULO: 6,
    Procedure.ELIMINAR_VEHICULO: 1,
    Procedure.CREAR_COMANDO: 4,
    Procedure.ACTUALIZAR_COMANDO: 5,
    Procedure.ELIMINAR_COMANDO: 1,
    Procedure.CREAR_DEMO: 3,
    Procedure.ACTUALIZAR_DEMO: 4,
    Procedure.ELIMINAR_DEMO: 1,
    Procedure.CREAR_MOVIMIENTO_DEMO: 5,
    Procedure.ACTUALIZAR_MOVIMIENTO_DEMO: 5,
    Procedure.ELIMINAR_MOVIMIENTO_DEMO: 1,
    Procedure.ACTUALIZAR_ESTADO: 3,
    Procedure.REGISTRAR_HISTORIAL: 3,
}


class ProcedureExecutor:
    """Ejecuta CALL con parámetros y agota todos sus resultados.

    La conexión la entrega el servicio: este objeto nunca abre, confirma ni
    revierte una transacción. Cada conjunto con columnas se conserva, también
    cuando está vacío. Se consume además el resultado final sin columnas de
    CALL para poder volver a utilizar la conexión inmediatamente.
    """

    def __init__(self, manager: MySQLConnectionManager) -> None:
        self.manager = manager

    @staticmethod
    def _consume(cursor: Any) -> list[list[dict[str, Any]]]:
        result_sets = []
        while True:
            if cursor.with_rows:
                result_sets.append(cursor.fetchall())
            if not cursor.nextset():
                break
        return result_sets

    def execute(
        self, connection: Any, procedure: Procedure, parameters: tuple = ()
    ) -> list[list[dict[str, Any]]]:
        if not isinstance(procedure, Procedure):
            raise ValueError("El procedimiento debe pertenecer a la lista interna permitida.")
        if not isinstance(parameters, tuple) or len(parameters) != _PARAMETER_COUNTS[procedure]:
            raise ValueError("Los parámetros no corresponden al contrato del procedimiento.")
        placeholders = ", ".join("%s" for _ in parameters)
        statement = f"CALL `{procedure.value}`({placeholders})"
        with self.manager.cursor(connection, dictionary=True) as cursor:
            cursor.execute(statement, parameters)
            return self._consume(cursor)

    def rows(self, connection: Any, procedure: Procedure, parameters: tuple = ()) -> list[dict[str, Any]]:
        """Primer conjunto con columnas; un conjunto vacío es una respuesta válida."""

        result_sets = self.execute(connection, procedure, parameters)
        return result_sets[0] if result_sets else []

    def identifier(self, connection: Any, procedure: Procedure, parameters: tuple, column: str) -> int:
        rows = self.rows(connection, procedure, parameters)
        identifier = rows[0].get(column) if rows else None
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
            raise DatabaseUnavailableError("query")
        return identifier


class VehiculoRepository:
    """Adapta los SP reales a métodos del dominio, sin reglas de negocio."""

    def __init__(self, manager: MySQLConnectionManager) -> None:
        self.manager = manager
        self.executor = ProcedureExecutor(manager)

    def listar_vehiculos(self, connection: Any) -> list[dict[str, Any]]:
        return self.executor.rows(connection, Procedure.LISTAR_VEHICULOS)

    def obtener_vehiculo(self, connection: Any, id_vehiculo: int) -> dict[str, Any] | None:
        rows = self.executor.rows(connection, Procedure.OBTENER_VEHICULO, (id_vehiculo,))
        return rows[0] if rows else None

    def obtener_comandos(self, connection: Any, id_vehiculo: int) -> list[dict[str, Any]]:
        return self.executor.rows(connection, Procedure.OBTENER_COMANDOS, (id_vehiculo,))

    def obtener_estado(self, connection: Any, id_vehiculo: int) -> dict[str, Any] | None:
        rows = self.executor.rows(connection, Procedure.OBTENER_ESTADO, (id_vehiculo,))
        return rows[0] if rows else None

    def obtener_historial(self, connection: Any, id_vehiculo: int) -> list[dict[str, Any]]:
        # El SP ordena; el servicio limita la representación a diez registros.
        return self.executor.rows(connection, Procedure.OBTENER_HISTORIAL, (id_vehiculo,))

    def obtener_demos(self, connection: Any, id_vehiculo: int) -> list[dict[str, Any]]:
        return self.executor.rows(connection, Procedure.OBTENER_DEMOS, (id_vehiculo,))

    def obtener_movimientos_demo(self, connection: Any, id_demo: int) -> list[dict[str, Any]]:
        return self.executor.rows(connection, Procedure.OBTENER_MOVIMIENTOS_DEMO, (id_demo,))

    def crear_vehiculo(
        self, connection: Any, nombre: str, modelo: str | None, descripcion: str | None,
        microcontrolador: str | None,
    ) -> int:
        return self.executor.identifier(
            connection, Procedure.CREAR_VEHICULO,
            (nombre, modelo, descripcion, microcontrolador), "id_vehiculo",
        )

    def actualizar_vehiculo(
        self, connection: Any, id_vehiculo: int, nombre: str, modelo: str | None,
        descripcion: str | None, microcontrolador: str | None, activo: bool,
    ) -> None:
        self.executor.execute(connection, Procedure.ACTUALIZAR_VEHICULO,
                              (id_vehiculo, nombre, modelo, descripcion, microcontrolador, activo))

    def eliminar_vehiculo(self, connection: Any, id_vehiculo: int) -> None:
        self.executor.execute(connection, Procedure.ELIMINAR_VEHICULO, (id_vehiculo,))

    def crear_comando(
        self, connection: Any, id_vehiculo: int, nombre: str, codigo: int, descripcion: str | None,
    ) -> int:
        return self.executor.identifier(connection, Procedure.CREAR_COMANDO,
                                        (id_vehiculo, nombre, codigo, descripcion), "id_comando")

    def actualizar_comando(
        self, connection: Any, id_comando: int, nombre: str, codigo: int,
        descripcion: str | None, activo: bool,
    ) -> None:
        self.executor.execute(connection, Procedure.ACTUALIZAR_COMANDO,
                              (id_comando, nombre, codigo, descripcion, activo))

    def eliminar_comando(self, connection: Any, id_comando: int) -> None:
        self.executor.execute(connection, Procedure.ELIMINAR_COMANDO, (id_comando,))

    def crear_demo(self, connection: Any, id_vehiculo: int, nombre: str, descripcion: str | None) -> int:
        return self.executor.identifier(connection, Procedure.CREAR_DEMO,
                                        (id_vehiculo, nombre, descripcion), "id_demo")

    def actualizar_demo(
        self, connection: Any, id_demo: int, nombre: str, descripcion: str | None, activo: bool,
    ) -> None:
        self.executor.execute(connection, Procedure.ACTUALIZAR_DEMO, (id_demo, nombre, descripcion, activo))

    def eliminar_demo(self, connection: Any, id_demo: int) -> None:
        self.executor.execute(connection, Procedure.ELIMINAR_DEMO, (id_demo,))

    def crear_movimiento_demo(
        self, connection: Any, id_demo: int, id_comando: int, orden: int,
        duracion_ms: int, pausa_ms: int,
    ) -> None:
        # Este SP no devuelve LAST_INSERT_ID; no se inventa una fila de salida.
        self.executor.execute(connection, Procedure.CREAR_MOVIMIENTO_DEMO,
                              (id_demo, id_comando, orden, duracion_ms, pausa_ms))

    def actualizar_movimiento_demo(
        self, connection: Any, id_demo_movimiento: int, id_comando: int, orden: int,
        duracion_ms: int, pausa_ms: int,
    ) -> None:
        self.executor.execute(connection, Procedure.ACTUALIZAR_MOVIMIENTO_DEMO,
                              (id_demo_movimiento, id_comando, orden, duracion_ms, pausa_ms))

    def eliminar_movimiento_demo(self, connection: Any, id_demo_movimiento: int) -> None:
        self.executor.execute(connection, Procedure.ELIMINAR_MOVIMIENTO_DEMO, (id_demo_movimiento,))

    def actualizar_estado(self, connection: Any, id_vehiculo: int, id_comando: int, conectado: bool) -> None:
        self.executor.execute(connection, Procedure.ACTUALIZAR_ESTADO, (id_vehiculo, id_comando, conectado))

    def registrar_historial(self, connection: Any, id_vehiculo: int, id_comando: int, origen: str) -> int:
        self.executor.execute(connection, Procedure.REGISTRAR_HISTORIAL, (id_vehiculo, id_comando, origen))
        # El SP inserta pero no retorna ID. LAST_INSERT_ID es propio de esta
        # sesión: no depende del orden ni de fechas previas del historial.
        with self.manager.cursor(connection, dictionary=True) as cursor:
            cursor.execute("SELECT LAST_INSERT_ID() AS id_historial")
            result_sets = ProcedureExecutor._consume(cursor)
        rows = result_sets[0] if result_sets else []
        identifier = rows[0].get("id_historial") if rows else None
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
            raise DatabaseUnavailableError("query")
        return identifier

    def _lock_row(self, connection: Any, statement: str, identifier: int) -> dict[str, Any] | None:
        with self.manager.cursor(connection, dictionary=True) as cursor:
            cursor.execute(statement, (identifier,))
            result_sets = ProcedureExecutor._consume(cursor)
        rows = result_sets[0] if result_sets else []
        return rows[0] if rows else None

    def bloquear_vehiculo(self, connection: Any, id_vehiculo: int) -> dict[str, Any] | None:
        return self._lock_row(connection,
                              "SELECT id_vehiculo FROM vehiculos WHERE id_vehiculo = %s FOR UPDATE",
                              id_vehiculo)

    def bloquear_comando(self, connection: Any, id_comando: int) -> dict[str, Any] | None:
        return self._lock_row(connection,
                              "SELECT id_comando FROM comandos WHERE id_comando = %s FOR UPDATE",
                              id_comando)

    def bloquear_estado(self, connection: Any, id_vehiculo: int) -> dict[str, Any] | None:
        return self._lock_row(connection,
                              "SELECT conectado FROM estado_vehiculo WHERE id_vehiculo = %s FOR UPDATE",
                              id_vehiculo)
