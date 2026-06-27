"""Concentration unit helpers for PHREEQC SOLUTION blocks."""
from __future__ import annotations

from .. import config

# Multiply a numeric value in `unit` by this factor to obtain mol/kgw (or mol/L).
_MOL_SCALE: dict[str, float] = {
    "mol/kgw": 1.0,
    "mmol/kgw": 1e-3,
    "umol/kgw": 1e-6,
    "mol/l": 1.0,
    "mmol/l": 1e-3,
    "umol/l": 1e-6,
}

# Mass- and ppm-based units are valid PHREEQC keywords but need per-species
# molar mass (or manual re-entry) to convert — not auto-converted here.
_MASS_UNITS = frozenset({"g/kgw", "mg/kgw", "ug/kgw", "g/l", "mg/l", "ug/l", "ppm"})


def is_mol_unit(unit: str) -> bool:
    return unit in _MOL_SCALE


def is_valid_unit(unit: str) -> bool:
    return unit in config.UNIT_OPTIONS


def convert_concentration(value: float, from_unit: str, to_unit: str) -> float | None:
    """Convert between mol-family units. Returns None if conversion is not defined."""
    if from_unit == to_unit:
        return value
    if from_unit not in _MOL_SCALE or to_unit not in _MOL_SCALE:
        return None
    # Same basis (kgw vs L) required; treated as equivalent for dilute aqueous solutions.
    if from_unit.endswith("/kgw") != to_unit.endswith("/kgw"):
        return None
    mol = value * _MOL_SCALE[from_unit]
    return mol / _MOL_SCALE[to_unit]
