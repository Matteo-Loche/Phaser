"""Dummy-electrolyte titration with selected solids allowed to precipitate."""
from __future__ import annotations

from .. import config
from .dummy_medium import BG_TITRANT_BASE, medium_lines
from .engine import (
    assemblage_solid_lines,
    format_assemblage_selected_output_suffix,
    totals_lines,
)
from .gas_limits import target_log_fo2

# Acidic seed (same as charge-balanced titration): redox is set by the O2(g) pin.
_SEED_PH = 1.8
_SEED_PE = 4.0


def format_assemblage_dummy_titration_input(
    *,
    ph: float,
    pe: float,
    params,
    flip_charge: bool = False,
) -> str:
    """Acidic seed + pH/O2 pins + selected solids at SI=0 / 0 moles initial.

    On PHREEQC failure the caller must retry once with ``flip_charge=True``.
    """
    lines = totals_lines(params)
    target_log_f_o2 = target_log_fo2(ph=ph, y=pe, params=params)
    bg = medium_lines(
        _SEED_PH,
        params.totals,
        molality=getattr(params, "background_molality", 0.0),
        flip=flip_charge,
    )
    solids = assemblage_solid_lines(params)
    return f"""TITLE Mineral stability (dummy titration assemblage)
SOLUTION 1
    temp      {params.temp_c:.6g}
    units     {config.DEFAULT_UNITS}
    water     {config.WATER_MASS_KGW:.12e}
    pH        {_SEED_PH}
    pe        {_SEED_PE}
{lines}
{bg}
END
USE solution 1
EQUILIBRIUM_PHASES 1
    Fix_H+ {-ph:.12g} {BG_TITRANT_BASE} 10
    -force_equality true
    O2(g) {target_log_f_o2:.12g} 10
    -force_equality true
{solids}END
{format_assemblage_selected_output_suffix(params)}"""
