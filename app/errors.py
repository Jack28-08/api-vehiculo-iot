"""Errores funcionales seguros, independientes de FastAPI y del conector MySQL."""


class ApplicationError(Exception):
    """Describe un rechazo esperado con un mensaje público, sin detalles SQL."""

    def __init__(self, status_code: int, message: str, code: str = "business_error") -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(message)
