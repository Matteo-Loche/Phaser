"""Real electrolyte titration with selected solids allowed to precipitate."""
from __future__ import annotations

from .. import config
from .engine import (
    assemblage_solid_lines,
    format_assemblage_selected_output_suffix,
    totals_lines,
)
from .gas_limits import target_log_fo2


def format_assemblage_titration_input(
    *,
    ph: float,
    pe: float,
    params,
) -> str:
    """Cl seed + Fix_H+/NaOH + O2 pin + selected solids at SI=0 / 0 moles initial."""
    lines = totals_lines(params)
    target_log_f_o2 = target_log_fo2(ph=ph, y=pe, params=params)
    solids = assemblage_solid_lines(params)
    return f"""TITLE Mineral stability (real electrolyte titration assemblage)
PHASES
Fix_H+
    H+ = H+
    log_k 0
SOLUTION 1
    temp      {params.temp_c:.6g}
    units     {config.DEFAULT_UNITS}
    water     {config.WATER_MASS_KGW:.12e}
    pH        1.8
    pe        4.0
{lines}
    Cl         1.0 charge
END
USE solution 1
EQUILIBRIUM_PHASES 1
    Fix_H+ {-ph:.12g} NaOH 10
    -force_equality true
    O2(g) {target_log_f_o2:.12g} 10
    -force_equality true
{solids}END
{format_assemblage_selected_output_suffix(params)}"""
