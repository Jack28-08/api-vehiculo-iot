"""Claves API por rol, independientes de la configuración y conexión MySQL."""

from enum import Enum
from functools import lru_cache
from pathlib import Path
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError


class Role(str, Enum):
    """El administrador también puede ejecutar las acciones del operador."""

    OPERATOR = "operator"
    ADMIN = "admin"


class ApiKeySettings(BaseSettings):
    """Carga secretos locales sin incluirlos en repr ni en errores de validación."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    api_key_operator: SecretStr = Field(default=SecretStr(""), repr=False)
    api_key_admin: SecretStr = Field(default=SecretStr(""), repr=False)


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Autenticación no disponible.",
    )


@lru_cache
def get_api_key_settings() -> ApiKeySettings:
    """Carga diferida: importar la aplicación no lee secretos ni abre MySQL."""

    try:
        return ApiKeySettings()
    except (ValidationError, SettingsError, OSError, UnicodeError):
        raise _unavailable() from None


api_key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ApiKey",
    auto_error=False,
    description=(
        "Clave API local. El operador ejecuta movimientos; el administrador "
        "gestiona recursos y también puede ejecutar movimientos."
    ),
)


def get_current_role(
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[ApiKeySettings, Depends(get_api_key_settings)],
) -> Role:
    """Valida ambas claves y compara bytes para admitir valores Unicode."""

    operator = settings.api_key_operator.get_secret_value()
    admin = settings.api_key_admin.get_secret_value()
    try:
        operator_bytes = operator.encode("utf-8")
        admin_bytes = admin.encode("utf-8")
        if not operator.strip() or not admin.strip() or secrets.compare_digest(
            operator_bytes, admin_bytes
        ):
            raise _unavailable()

        supplied = (api_key or "").encode("utf-8")
        is_operator = secrets.compare_digest(supplied, operator_bytes)
        is_admin = secrets.compare_digest(supplied, admin_bytes)
    except UnicodeError:
        raise _unavailable() from None

    if is_admin:
        return Role.ADMIN
    if is_operator:
        return Role.OPERATOR
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Clave API ausente o inválida.",
        headers={"WWW-Authenticate": "ApiKey"},
    )


def require_operator(role: Annotated[Role, Depends(get_current_role)]) -> Role:
    """Autoriza movimientos a operadores y administradores."""

    return role


def require_admin(role: Annotated[Role, Depends(get_current_role)]) -> Role:
    """Autoriza la gestión de recursos exclusivamente al administrador."""

    if role is not Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permisos insuficientes.",
        )
    return role
