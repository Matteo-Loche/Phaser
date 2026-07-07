"""Direct fixed-pH/pe PHREEQC input (no charge-balance titration)."""
from __future__ import annotations

from .. import config
from .engine import format_selected_output_suffix, totals_lines


def format_direct_input(
    *,
    ph: float,
    pe: float,
    params,
) -> str:
    """Single SOLUTION at grid (pH, pe) with user totals; electroneutrality not enforced."""
    lines = totals_lines(params)
    body = f"""TITLE Phase diagram (direct)
SOLUTION 1
    temp      {params.temp_c:.6g}
    pH        {ph:.12g}
    pe        {pe:.12g}
    units     {config.DEFAULT_UNITS}
{lines}
END
USE solution 1
{format_selected_output_suffix(params)}"""
    return body
