"""Configuración privada de MySQL; cargarla no abre conexiones."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Lee MYSQL_* desde el entorno o .env sin valores de acceso en el código."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    mysql_host: str = Field(min_length=1, repr=False)
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = Field(min_length=1, repr=False)
    mysql_password: SecretStr = Field(min_length=1, repr=False)
    mysql_database: str = Field(min_length=1, repr=False)


@lru_cache
def get_settings() -> Settings:
    """Carga la configuración cuando se solicite, sin abrir conexiones."""
    return Settings()
