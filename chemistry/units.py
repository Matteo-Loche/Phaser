"""Concentration unit helpers for PHREEQC SOLUTION blocks."""
from __future__ import annotations

from .. import config

# Canonical molality units (mol/kgw basis). PHREEQC input always uses mmol/kgw.
_MOL_SCALE_KGW: dict[str, float] = {
    "mol/kgw": 1.0,
    "mmol/kgw": 1e-3,
    "umol/kgw": 1e-6,
}

# UI unit aliases (e.g. µmol/kgw) normalized to canonical keys.
_UNIT_ALIASES = {
    "µmol/kgw": "umol/kgw",
    "μmol/kgw": "umol/kgw",
}


def normalize_unit(unit: str) -> str:
    """Map UI aliases (e.g. µmol/kgw) to canonical unit keys."""
    u = (unit or "").strip()
    return _UNIT_ALIASES.get(u, u)


def unit_label(unit: str) -> str:
    """Human-readable label (µ symbol for micromoles)."""
    u = normalize_unit(unit)
    if u == "umol/kgw":
        return "µmol/kgw"
    return u


def is_valid_unit(unit: str) -> bool:
    return normalize_unit(unit) in config.UNIT_OPTIONS


def convert_concentration(value: float, from_unit: str, to_unit: str) -> float | None:
    """Convert between mol/kgw, mmol/kgw, and umol/kgw."""
    src = normalize_unit(from_unit)
    dst = normalize_unit(to_unit)
    if src == dst:
        return value
    if src not in _MOL_SCALE_KGW or dst not in _MOL_SCALE_KGW:
        return None
    mol = value * _MOL_SCALE_KGW[src]
    return mol / _MOL_SCALE_KGW[dst]


def to_mmol_kgw(value: float, unit: str) -> float:
    """Convert a concentration to mmol/kgw for PHREEQC SOLUTION input."""
    u = normalize_unit(unit)
    if u not in _MOL_SCALE_KGW:
        raise ValueError(f"Unsupported concentration unit: {unit!r}")
    converted = convert_concentration(value, u, config.DEFAULT_UNITS)
    assert converted is not None
    return converted


def totals_to_mmol_kgw(totals: dict[str, float], unit: str) -> dict[str, float]:
    """Convert all total concentrations to mmol/kgw."""
    return {name: to_mmol_kgw(val, unit) for name, val in totals.items() if val > 0}


_DISPLAY_DECIMALS: dict[str, int] = {
    "mol/kgw": 6,
    "mmol/kgw": 4,
    "umol/kgw": 2,
}


def round_for_unit(value: float, unit: str) -> float:
    """Round a concentration for display / UI after unit conversion."""
    u = normalize_unit(unit)
    if u not in _DISPLAY_DECIMALS:
        raise ValueError(f"Unsupported concentration unit: {unit!r}")
    return round(float(value), _DISPLAY_DECIMALS[u])
