"""Generic PHREEQC pe–pH grid evaluation."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..phreeqc.catalog import is_gas


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
    # Extra aqueous species for SELECTED_OUTPUT -mol (boundary tracing).
    aq_species_molality: tuple[str, ...] = ()
    # Override TOP_AQ_SPECIES_PER_ELEMENT for this job (trace uses fewer).
    top_aq_species_per_element: int | None = None
    # Phase names that also occur as aqueous species names (from catalog scan).
    solid_aqueous_collisions: tuple[str, ...] = ()
    # Catalog-derived eligible solid phases per element subset key.
    phase_names_by_subset: dict[str, tuple[str, ...]] = field(default_factory=dict)
    gas_phases: tuple[str, ...] = ()


_TUPLE_FIELDS = (
    "phases",
    "system_elements",
    "aq_species_molality",
    "solid_aqueous_collisions",
    "gas_phases",
)


def grid_job_params_from_dict(data: dict) -> GridJobParams:
    """Rebuild params after process-pool JSON/asdict round-trip."""
    kwargs = dict(data)
    for key in _TUPLE_FIELDS:
        if key in kwargs and kwargs[key] is not None:
            kwargs[key] = tuple(kwargs[key])
    pnbs = kwargs.get("phase_names_by_subset")
    if isinstance(pnbs, dict):
        kwargs["phase_names_by_subset"] = {
            str(k): tuple(v) for k, v in pnbs.items()
        }
    return GridJobParams(**kwargs)


@dataclass
class GridPointResult:
    ph: float
    pe: float
    converged: bool
    dominant_phase: str = "aqueous"
    dominant_solid: str = "aqueous"
    dominant_aq_by_element: dict[str, str] = field(default_factory=dict)
    aq_molality_by_element: dict[str, float] = field(default_factory=dict)
    aq_molality_by_species: dict[str, float] = field(default_factory=dict)
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


def _top_aq_species_per_element(params: GridJobParams) -> int:
    if params.top_aq_species_per_element is not None:
        return params.top_aq_species_per_element
    return config.TOP_AQ_SPECIES_PER_ELEMENT


def _format_user_punch(elements: tuple[str, ...], *, top_n: int) -> str:
    if not elements:
        return ""
    headings = []
    for elem in elements:
        headings.extend([f"dom_{elem}", f"m_dom_{elem}"])
        for k in range(1, top_n + 1):
            headings.extend([f"sp_{elem}_{k}", f"m_{elem}_{k}"])
    lines = ["USER_PUNCH", f"    -headings {' '.join(headings)}", "    -start"]
    base = 1000
    for idx, elem in enumerate(elements):
        b = base + idx * 200
        pad = b + 80
        cont = b + 100
        lines.extend(
            [
                f"{b} n = SYS(\"{elem}\", n, nm$, ty$, mo)",
                f"{b + 10} IF n > 0 THEN PUNCH nm$(1) ELSE PUNCH \"none\"",
                f"{b + 20} IF n > 0 THEN PUNCH mo(1) ELSE PUNCH -999",
                f"{b + 30} FOR ii = 1 TO {top_n}",
                f"{b + 40} IF ii > n THEN GOTO {pad}",
                f"{b + 50} PUNCH nm$(ii)",
                f"{b + 60} PUNCH mo(ii)",
                f"{b + 70} GOTO {cont}",
                f"{pad} PUNCH \"none\"",
                f"{pad + 10} PUNCH -999",
                f"{cont} NEXT ii",
            ]
        )
    lines.append("    -end")
    return "\n".join(lines) + "\n"


def _mol_headers(species: str) -> list[str]:
    """Candidate SELECTED_OUTPUT column names for a species molality."""
    return [f"m_{species}", f"mol_{species}", f"Mola_{species}", species]


def _parse_species_molalities(row: dict, params: GridJobParams) -> dict[str, float]:
    """Merge USER_PUNCH top-species slots and explicit -mol species."""
    out: dict[str, float] = {}
    top_n = _top_aq_species_per_element(params)
    for elem in params.system_elements:
        for k in range(1, top_n + 1):
            sp = _row_str(row, f"sp_{elem}_{k}", default="")
            if not sp or sp == "none":
                continue
            m = _row_value(row, f"m_{elem}_{k}")
            if m == m and m > 0:
                out[sp] = m
    for sp in params.aq_species_molality:
        for key in _mol_headers(sp):
            m = _row_value(row, key)
            if m == m and m > 0:
                out[sp] = m
                break
    return out


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
    user_punch = _format_user_punch(
        params.system_elements,
        top_n=_top_aq_species_per_element(params),
    )
    mol_line = ""
    if params.aq_species_molality:
        mol_tokens = " ".join(
            f'"{s}"' if " " in s or "(" in s or "-" in s else s
            for s in params.aq_species_molality
        )
        mol_line = f"    -mol {mol_tokens}\n"
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
{mol_line}
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
        sp_mol = _parse_species_molalities(row, params)
        for elem in params.system_elements:
            species = _row_str(row, f"dom_{elem}", default="none")
            if species != "none":
                dominant_aq[elem] = species
            m = _row_value(row, f"m_dom_{elem}")
            if m == m:
                mol_aq[elem] = m
                if species != "none" and species not in sp_mol:
                    sp_mol[species] = m

        return GridPointResult(
            ph=ph,
            pe=pe,
            converged=True,
            si=si,
            dominant_phase=_dominant_from_si(si, include=solid_phases, default="aqueous"),
            dominant_solid=_dominant_from_si(si, include=solid_phases, default="aqueous"),
            dominant_aq_by_element=dominant_aq,
            aq_molality_by_element=mol_aq,
            aq_molality_by_species=sp_mol,
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
