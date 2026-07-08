"""Dummy-electrolyte titration PHREEQC input (Fix_H+/BgcOH + O2, no solids)."""
from __future__ import annotations

from .. import config
from .dummy_medium import BG_TITRANT_BASE, medium_lines
from .engine import format_selected_output_suffix, totals_lines
from .gas_limits import log_f_o2

# Acidic seed (same as charge-balanced titration): redox is set by the O2(g) pin.
_SEED_PH = 1.8
_SEED_PE = 4.0


def format_dummy_titration_input(
    *,
    ph: float,
    pe: float,
    params,
    flip_charge: bool = False,
) -> str:
    """Acidic seed + pH/O2 titration via dummy medium (predominance mode).

    The seed pH/pe are not the grid targets; Fix_H+ and O2(g) pin the diagram
    point. Charge balance uses the seed pH for the first-side guess.

    On PHREEQC failure the caller must retry once with ``flip_charge=True``.
    """
    lines = totals_lines(params)
    target_log_f_o2 = log_f_o2(ph=ph, pe=pe, temp_c=params.temp_c)
    bg = medium_lines(
        _SEED_PH,
        params.totals,
        molality=getattr(params, "background_molality", 0.0),
        flip=flip_charge,
    )
    return f"""TITLE Phase diagram (dummy titration)
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
END
{format_selected_output_suffix(params)}"""
