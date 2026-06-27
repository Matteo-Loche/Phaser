"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import config


class TotalsModel(BaseModel):
    totals: dict[str, float] = Field(default_factory=dict)
    charge_species: str = "Na"


class PhaseQuery(BaseModel):
    db_id: str | None = None
    db_path: str | None = None  # legacy migration aid; must match a registered database
    elements: list[str]
    selected: list[str] | None = None
    exclude_element_solids: bool = True
    exclude_gases: bool = False


class ComputeRequest(BaseModel):
    db_id: str | None = None
    db_path: str | None = None  # legacy migration aid; must match a registered database
    dll_path: str | None = None
    temp_c: float = config.TEMP_C
    ph_min: float = config.PH_MIN
    ph_max: float = config.PH_MAX
    ph_levels: int = config.PH_LEVELS
    pe_min: float = config.PE_MIN
    pe_max: float = config.PE_MAX
    pe_levels: int = config.PE_LEVELS
    totals: dict[str, float]
    charge_species: str = "Na"
    units: str = config.DEFAULT_UNITS
    phases: list[str] | None = None
    system_elements: list[str] | None = None
    exclude_gases: bool = True
    include_common_gases: bool = False
    gas_phases: list[str] | None = None
    max_workers: int | None = None


class RegisterDatabaseRequest(BaseModel):
    """Register metadata for a generated .dat file already placed on the server."""
    filename: str
    metadata: dict[str, Any] = Field(default_factory=dict)
