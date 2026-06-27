"""Generic PHREEQC pe–pH grid evaluation."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..db.parser import is_gas


@dataclass
class GridJobParams:
    db_path: str
    dll_path: str
    temp_c: float
    ph_min: float
    ph_max: float
    ph_levels: int
    pe_min: float
    pe_max: float
    pe_levels: int
    totals: dict[str, float]
    phases: tuple[str, ...]
    system_elements: tuple[str, ...] = ()
    charge_species: str = "Na"
    units: str = config.DEFAULT_UNITS


@dataclass
class GridPointResult:
    ph: float
    pe: float
    converged: bool
    dominant_phase: str = "aqueous"
    dominant_solid: str = "aqueous"
    dominant_aq_by_element: dict[str, str] = field(default_factory=dict)
    aq_molality_by_element: dict[str, float] = field(default_factory=dict)
    si: dict[str, float] = field(default_factory=dict)


def python_is_64bit() -> bool:
    return struct.calcsize("P") * 8 == 64


def validate_phreeqc_setup(dll_path: str, db_path: str) -> None:
    """Fail fast with a clear message before spawning worker processes."""
    lib = Path(dll_path)
    db = Path(db_path)
    if not lib.is_file():
        raise RuntimeError(
            f"IPhreeqc library not found: {dll_path}\n"
            "On Linux/WSL, build IPhreeqc from source (see phreeqpy docs) and set "
            "PHASER_IPHREEQC_LIB to the full path of libiphreeqc.so."
        )
    if not db.is_file():
        raise RuntimeError(
            f"PHREEQC database not found: {db_path}\n"
            "Set PHASER_DB to the full path of your .dat file."
        )
    init_phreeqc(dll_path, db_path)


def init_phreeqc(dll_path: str, db_path: str):
    from phreeqpy.iphreeqc import phreeqc_dll as phreeqc_dll_mod

    try:
        pq = phreeqc_dll_mod.IPhreeqc(dll_path=str(dll_path))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load IPhreeqc from {dll_path}. "
            f"Python is {'64-bit' if python_is_64bit() else '32-bit'}.\n{exc}"
        ) from exc
    pq.load_database(str(db_path))
    return pq


def _float_or_nan(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    if out <= -999:
        return float("nan")
    return out


def _row_value(row: dict, *keys: str) -> float:
    for key in keys:
        if key in row:
            return _float_or_nan(row[key])
    return float("nan")


def _row_str(row: dict, key: str, default: str = "none") -> str:
    value = row.get(key)
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan"}:
        return default
    return text


def _dominant_from_si(si: dict[str, float], *, include: set[str] | None = None, default: str = "aqueous") -> str:
    finite = {
        k: v for k, v in si.items()
        if v == v and (include is None or k in include)
    }
    if not finite:
        return default
    phase, value = max(finite.items(), key=lambda kv: kv[1])
    return phase if value >= 0.0 else default


def _format_user_punch(elements: tuple[str, ...]) -> str:
    if not elements:
        return ""
    headings = []
    for elem in elements:
        headings.extend([f"dom_{elem}", f"m_dom_{elem}"])
    lines = ["USER_PUNCH", f"    -headings {' '.join(headings)}", "    -start"]
    ln = 10
    for elem in elements:
        lines.append(f"{ln} t = SYS(\"{elem}\", n, nm$, ty$, mo)")
        ln += 10
        lines.append(f"{ln} IF n > 0 THEN PUNCH nm$(1) ELSE PUNCH \"none\"")
        ln += 10
        lines.append(f"{ln} IF n > 0 THEN PUNCH mo(1) ELSE PUNCH -999")
        ln += 10
    lines.append("    -end")
    return "\n".join(lines) + "\n"


def format_grid_input(
    *,
    ph: float,
    pe: float,
    params: GridJobParams,
) -> str:
    totals_lines = "\n".join(
        f"    {name:<10} {val:.12e}" for name, val in sorted(params.totals.items()) if val > 0
    )
    si_list = " ".join(f'"{p}"' if " " in p or "(" in p else p for p in params.phases)
    charge = params.charge_species
    user_punch = _format_user_punch(params.system_elements)
    return f"""
TITLE Phase diagram
SOLUTION 1
    temp      {params.temp_c:.6g}
    units     {params.units}
    water     {config.WATER_MASS_KGW:.12e}
    pH        {ph:.12g}
    pe        {pe:.12g}
{totals_lines}
    {charge:<10} 0.0 charge
    Cl         0.0

SELECTED_OUTPUT
    -reset false
    -pH true
    -pe true
    -si {si_list}

{user_punch}END
"""


def evaluate_point(phreeqc, *, ph: float, pe: float, params: GridJobParams) -> GridPointResult:
    base = GridPointResult(ph=ph, pe=pe, converged=False)
    try:
        phreeqc.run_string(format_grid_input(ph=ph, pe=pe, params=params))
        selected = phreeqc.get_selected_output_array()
        if not selected or len(selected) < 2:
            return base
        headers = selected[0]
        row = dict(zip(headers, selected[1]))
        si = {phase: _row_value(row, f"si_{phase}") for phase in params.phases}

        solid_phases = {p for p in params.phases if not is_gas(p)}

        dominant_aq: dict[str, str] = {}
        mol_aq: dict[str, float] = {}
        for elem in params.system_elements:
            species = _row_str(row, f"dom_{elem}", default="none")
            if species != "none":
                dominant_aq[elem] = species
            m = _row_value(row, f"m_dom_{elem}")
            if m == m:
                mol_aq[elem] = m

        return GridPointResult(
            ph=ph,
            pe=pe,
            converged=True,
            si=si,
            dominant_phase=_dominant_from_si(si, include=solid_phases, default="aqueous"),
            dominant_solid=_dominant_from_si(si, include=solid_phases, default="aqueous"),
            dominant_aq_by_element=dominant_aq,
            aq_molality_by_element=mol_aq,
        )
    except Exception:
        return base


def eh_from_pe(pe: float, temp_c: float) -> float:
    """Convert pe to Eh (V) at temperature T (°C)."""
    t_k = temp_c + 273.15
    return pe * 2.303 * 8.314462618 * t_k / 96485.33212


def element_from_total_key(key: str) -> str:
    """Extract element symbol from a PHREEQC total key (e.g. C(4) -> C)."""
    m = re.match(r"^([A-Z][a-z]?)", key.strip())
    return m.group(1) if m else key
