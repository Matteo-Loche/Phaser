"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .. import config
from ..chemistry.units import is_valid_unit, normalize_unit


class PhaseQuery(BaseModel):
    db_id: str | None = None
    db_path: str | None = None  # optional; must match a registered database (prefer db_id)
    elements: list[str]
    selected: list[str] | None = None
    exclude_element_solids: bool = True
    exclude_gases: bool = False


class ComputeRequest(BaseModel):
    db_id: str | None = None
    db_path: str | None = None  # optional; must match a registered database (prefer db_id)
    temp_c: float = config.TEMP_C
    ph_min: float = config.PH_MIN
    ph_max: float = config.PH_MAX
    ph_levels: int = Field(default=config.GRID_LEVELS, ge=config.MIN_GRID_LEVELS, le=config.MAX_GRID_LEVELS)
    pe_min: float = config.PE_MIN
    pe_max: float = config.PE_MAX
    pe_levels: int = Field(default=config.GRID_LEVELS, ge=config.MIN_GRID_LEVELS, le=config.MAX_GRID_LEVELS)
    totals: dict[str, float]
    units: str = config.DEFAULT_UNITS
    phases: list[str] | None = None
    system_elements: list[str] | None = None
    exclude_gases: bool = True
    include_common_gases: bool = False
    gas_phases: list[str] | None = None
    adaptive_boundaries: bool = config.ADAPTIVE_BOUNDARIES_DEFAULT
    adaptive_refine_factor: int | None = None
    o2_limit_atm: float = config.O2_FUGACITY_LIMIT_ATM
    h2_limit_atm: float = config.H2_FUGACITY_LIMIT_ATM
    layer_solids: bool = True
    layer_aqueous: bool = True
    layer_elements: bool = False
    solution_mode: str = config.SOLUTION_MODE_DEFAULT
    mineral_category_mode: str = config.MINERAL_CATEGORY_MODE_DEFAULT
    knobs_mode: str = config.KNOBS_MODE_DEFAULT

    @field_validator("solution_mode")
    @classmethod
    def _validate_solution_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in config.SOLUTION_MODES:
            allowed = ", ".join(config.SOLUTION_MODES)
            raise ValueError(f"Unsupported solution_mode: {value!r}. Use one of: {allowed}.")
        return mode

    @field_validator("mineral_category_mode")
    @classmethod
    def _validate_mineral_category_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in config.MINERAL_CATEGORY_MODES:
            allowed = ", ".join(config.MINERAL_CATEGORY_MODES)
            raise ValueError(
                f"Unsupported mineral_category_mode: {value!r}. Use one of: {allowed}."
            )
        return mode

    @field_validator("knobs_mode")
    @classmethod
    def _validate_knobs_mode(cls, value: str) -> str:
        return config.normalize_knobs_mode(value)

    @field_validator("units")
    @classmethod
    def _validate_units(cls, value: str) -> str:
        unit = normalize_unit(value)
        if not is_valid_unit(unit):
            raise ValueError(
                f"Unsupported concentration unit: {value!r}. "
                f"Use one of: {', '.join(config.UNIT_OPTIONS)}."
            )
        return unit

    @model_validator(mode="after")
    def _at_least_one_layer(self) -> ComputeRequest:
        if not (self.layer_solids or self.layer_aqueous):
            raise ValueError(
                "Enable at least one layer family "
                "(layer_solids or layer_aqueous)."
            )
        return self


class RegisterDatabaseRequest(BaseModel):
    """Register metadata for a generated .dat file already placed on the server."""
    filename: str
    metadata: dict[str, Any] = Field(default_factory=dict)
