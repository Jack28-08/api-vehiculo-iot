"""Entradas de Administración 4.3."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator


ID = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
LegacyID = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
Code = Annotated[int, Field(strict=True, ge=-(2**31), le=2**31 - 1)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, max_length=254,
                  pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")]
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]
CommercialState = Literal["inventario", "exhibicion", "vendida", "inactiva"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CapabilityInput(Input):
    codigo: Code
    control: StrictBool = True
    invitado: StrictBool = False

    @model_validator(mode="after")
    def limited_guest(self):
        if self.invitado and not self.control:
            raise ValueError("Una capacidad de invitado también debe estar habilitada.")
        return self


class ModelInput(Input):
    sku: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40,
                   pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    nombre: Short
    descripcion: Description | None = None
    publicado: StrictBool = False
    activo: StrictBool = True
    capacidades: list[CapabilityInput] = Field(default_factory=list, max_length=50)

    @field_validator("capacidades")
    @classmethod
    def unique_codes(cls, rows):
        if len({row.codigo for row in rows}) != len(rows):
            raise ValueError("Las capacidades deben usar códigos distintos.")
        return rows


class UnitInput(Input):
    id_vehiculo: LegacyID | None = None
    id_modelo: ID
    serie: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    nombre: Short
    estado: CommercialState = "inventario"
    fisico: StrictBool = False
    version: ID | None = None

    @model_validator(mode="after")
    def physical_link(self):
        if self.fisico and self.id_vehiculo is None:
            raise ValueError("La unidad física requiere un vehículo IoT existente.")
        return self


class ClientInput(Input):
    correo: Email
    nombre: Short
    contrasena: Password


class ClientStatus(Input):
    activo: StrictBool


class AssignmentInput(Input):
    id_cliente: ID | None
    version: ID


class ReservationInput(Input):
    id_unidad: ID


class DemoStep(Input):
    codigo: Code
    duracion_ms: Annotated[int, Field(strict=True, ge=1, le=5000)]
    pausa_ms: Annotated[int, Field(strict=True, ge=0, le=5000)] = 0


class DemoInput(Input):
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    descripcion: Description | None = None
    activo: StrictBool = True
    id_modelo: ID | None = None
    id_unidad: ID | None = None
    version: ID | None = None
    pasos: list[DemoStep] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def duration_and_scope(self):
        if (self.id_modelo is None) == (self.id_unidad is None):
            raise ValueError("Selecciona un modelo de fábrica o una unidad propia.")
        if sum(step.duracion_ms + step.pausa_ms for step in self.pasos) > 60_000:
            raise ValueError("La DEMO debe durar como máximo 60 segundos.")
        return self


class CapabilityView(BaseModel):
    codigo: int
    control: bool
    invitado: bool


class ModelView(BaseModel):
    id: int
    sku: str
    nombre: str
    descripcion: str | None
    publicado: bool
    activo: bool
    capacidades: list[CapabilityView]


class UnitView(BaseModel):
    id: int
    id_vehiculo: int | None
    id_modelo: int
    serie: str
    nombre: str
    estado: CommercialState
    fisico: bool
    version: int
    modelo: str


class InventoryUnitView(UnitView):
    id_cliente: int | None
    id_titularidad: int | None
    modelo_activo: bool
    despacho: str | None = None


class PublicUnitView(BaseModel):
    id: int
    nombre: str
    disponible: bool


class PublicModelView(BaseModel):
    id: int
    sku: str
    nombre: str
    descripcion: str | None
    capacidades: list[CapabilityView]
    unidades: list[PublicUnitView]


class ReservationUnitView(BaseModel):
    id: int
    id_vehiculo: int
    id_modelo: int
    nombre: str
    fisico: Literal[False]
    modelo: str
    version: int


class ReservationIssued(BaseModel):
    token: str
    expires_at: datetime
    id_reserva: int
    unidad: ReservationUnitView


class ReservationView(BaseModel):
    id: int
    id_unidad: int
    unidad: str
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    activa: bool


class OwnershipView(BaseModel):
    id: int
    id_cliente: int
    cliente: str
    id_actor: int
    inicio: datetime
    fin: datetime | None


class ClientView(BaseModel):
    id: int
    correo: str
    nombre: str
    activo: bool


class DashboardView(BaseModel):
    modelos_activos: int
    unidades_activas: int
    unidades_inventario: int
    unidades_vendidas: int
    unidades_exhibicion: int
    clientes_activos: int
    demos_activas: int
    reservas_activas: int


class AuditView(BaseModel):
    id: int
    id_actor: int | None
    actor: str | None
    accion: str
    recurso: str
    id_recurso: int | None
    resultado: str
    fecha: datetime


class DemoStepView(BaseModel):
    orden: int
    codigo: int
    duracion_ms: int
    pausa_ms: int


class DemoView(BaseModel):
    id: int
    id_unidad: int | None
    id_modelo: int | None
    nombre: str
    descripcion: str | None
    tipo: Literal["fabrica", "privada"]
    activo: bool
    version: int
    pasos: list[DemoStepView]
    total_ms: int
