"""Conexiones independientes y transacciones explícitas para MySQL/RDS."""

from collections.abc import Iterator
from contextlib import contextmanager
import socket
from typing import Any

import mysql.connector
from mysql.connector import errors

from app.config.settings import Settings, get_settings


_MESSAGES = {
    "configuration": "La configuración de MySQL está incompleta o no es válida.",
    "dns": "No se pudo resolver el servidor de la base de datos.",
    "timeout": "La base de datos no respondió dentro del tiempo permitido.",
    "network": "No se pudo establecer o mantener la conexión con la base de datos.",
    "authentication": "La base de datos rechazó la autenticación.",
    "database_missing": "La base de datos configurada no está disponible.",
    "permission": "La cuenta no tiene permiso para realizar esta consulta.",
    "tls": "No se pudo establecer una conexión segura con la base de datos.",
    "conflict": "La operación entra en conflicto con los datos o sus relaciones.",
    "validation": "Los valores no son válidos para el contrato de la base de datos.",
    "query": "No se pudo completar la consulta a la base de datos.",
    "unknown": "La base de datos no está disponible.",
}


class DatabaseUnavailableError(Exception):
    """Error público sin el texto original del conector ni credenciales.

    ``message``, si se proporciona, debe ser un texto fijo apto para publicar.
    El administrador utiliza exclusivamente los mensajes definidos arriba.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code if code in _MESSAGES else "unknown"
        self.message = message if message is not None else _MESSAGES[self.code]
        super().__init__(self.message)


def _safe_error(error: Exception, *, default: str = "unknown") -> DatabaseUnavailableError:
    """Clasifica internamente; nunca devuelve el mensaje recibido del servidor."""

    if isinstance(error, DatabaseUnavailableError):
        return error
    errno = getattr(error, "errno", None)
    if isinstance(error, socket.gaierror) or errno == 2005:
        code = "dns"
    elif isinstance(
        error,
        (TimeoutError, errors.ConnectionTimeoutError, errors.ReadTimeoutError, errors.WriteTimeoutError),
    ):
        code = "timeout"
    elif errno in (1045, 1698):
        code = "authentication"
    elif errno == 1049:
        code = "database_missing"
    elif errno in (1044, 1142, 1143, 1227, 1370):
        code = "permission"
    elif errno in (2026, 3159):
        code = "tls"
    elif errno in (1264, 1265, 1366, 1406, 3819):
        code = "validation"
    elif errno in (1062, 1451, 1452) or isinstance(error, errors.IntegrityError):
        code = "conflict"
    elif errno in (2002, 2003, 2006, 2013, 2055):
        # Algunas versiones del conector envuelven un timeout en OperationalError.
        # El texto se consulta sólo para clasificarlo y nunca se publica.
        internal_message = str(error).lower()
        code = "timeout" if "timeout" in internal_message or "timed out" in internal_message else "network"
    elif isinstance(error, mysql.connector.Error):
        code = "query" if default == "query" else default
    elif isinstance(error, OSError):
        code = "network"
    else:
        code = default
    return DatabaseUnavailableError(code)


class MySQLConnectionManager:
    """Abre una conexión por operación; no comparte sesiones entre solicitudes.

    El acceso es síncrono. Las rutas de FastAPI deben usar ``def`` o delegarlo
    al grupo de hilos. Las escrituras se coordinan mediante ``transaction()``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        connection_timeout: int = 5,
        read_timeout: int = 10,
        write_timeout: int = 10,
    ) -> None:
        self._settings = settings
        timeouts = (connection_timeout, read_timeout, write_timeout)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in timeouts):
            raise DatabaseUnavailableError("configuration") from None
        self._connection_timeout = connection_timeout
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

    def _connection_options(self) -> dict[str, Any]:
        try:
            settings = self._settings if self._settings is not None else get_settings()
            return {
                "host": settings.mysql_host,
                "port": settings.mysql_port,
                "user": settings.mysql_user,
                "password": settings.mysql_password.get_secret_value(),
                "database": settings.mysql_database,
                "connection_timeout": self._connection_timeout,
                "read_timeout": self._read_timeout,
                "write_timeout": self._write_timeout,
                "autocommit": False,
                "time_zone": "+00:00",
            }
        except Exception:
            # Incluso ValidationError puede contener valores de entrada.
            raise DatabaseUnavailableError("configuration") from None

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Abre y cierra una conexión nueva, incluso si la operación falla."""

        options = self._connection_options()
        try:
            connection = mysql.connector.connect(**options)
        except Exception as error:
            raise _safe_error(error) from None
        completed = False
        try:
            yield connection
            completed = True
        except mysql.connector.Error as error:
            raise _safe_error(error, default="query") from None
        finally:
            try:
                connection.close()
            except Exception as error:
                # Conserva el error de la operación si también falla el cierre.
                if completed:
                    raise _safe_error(error) from None

    @contextmanager
    def cursor(self, connection: Any, *, dictionary: bool = False) -> Iterator[Any]:
        """Cierra el cursor independientemente del resultado de la consulta."""

        try:
            cursor = connection.cursor(dictionary=dictionary)
        except Exception as error:
            raise _safe_error(error, default="query") from None
        completed = False
        try:
            yield cursor
            completed = True
        except mysql.connector.Error as error:
            raise _safe_error(error, default="query") from None
        finally:
            try:
                cursor.close()
            except Exception as error:
                if completed:
                    raise _safe_error(error, default="query") from None

    @contextmanager
    def transaction(self, *, readonly: bool = False) -> Iterator[Any]:
        """Confirma al terminar; ante cualquier fallo intenta revertir y cerrar.

        ``readonly=True`` solicita a MySQL una transacción de sólo lectura.
        No se reintentan escrituras automáticamente: su resultado podría ser
        incierto si se interrumpe la conexión durante ``commit``.
        """

        with self.connection() as connection:
            try:
                connection.start_transaction(readonly=readonly)
                yield connection
                connection.commit()
            except BaseException as error:
                try:
                    connection.rollback()
                except Exception:
                    pass  # El contexto exterior todavía garantiza intentar close().
                if isinstance(error, mysql.connector.Error):
                    raise _safe_error(error, default="query") from None
                raise

    def check_connection(self) -> None:
        """Consulta real de lectura sin acceder a las tablas de negocio."""

        with self.connection() as connection:
            with self.cursor(connection) as cursor:
                cursor.execute("SELECT 1 AS ok")
                row = cursor.fetchone()
                if row is None or row[0] != 1:
                    raise DatabaseUnavailableError("query") from None
