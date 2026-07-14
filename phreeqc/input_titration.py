"""Real electrolyte titration PHREEQC input (Fix_H+/NaOH + O2 fugacity).

Cl⁻/Na⁺ keep the solution electroneutral; their inclusion may alter speciation.
"""
from __future__ import annotations

from .. import config
from .engine import format_selected_output_suffix, totals_lines
from .gas_limits import log_f_o2


def format_titration_input(
    *,
    ph: float,
    pe: float,
    params,
) -> str:
    """Real-electrolyte titration: acidic Cl seed + Fix_H+/NaOH + fixed O2 fugacity."""
    lines = totals_lines(params)
    target_log_f_o2 = log_f_o2(ph=ph, pe=pe, temp_c=params.temp_c)
    return f"""TITLE Phase diagram (real electrolyte titration)
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
END
{format_selected_output_suffix(params)}"""
