"""Generic PHREEQC pe–pH grid evaluation."""
from __future__ import annotations

import math
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
    # Component gases traced via SI(gas) - log10(P_ref) (O2/H2 use analytic limits).
    trace_gas_phases: tuple[str, ...] = ()
    o2_limit_atm: float = config.O2_FUGACITY_LIMIT_ATM
    h2_limit_atm: float = config.H2_FUGACITY_LIMIT_ATM
    component_gas_limit_atm: float = config.COMPONENT_GAS_FUGACITY_LIMIT_ATM
    # Which diagram layer families to pack / trace (all on by default).
    layer_solids: bool = True
    layer_aqueous: bool = True
    layer_elements: bool = True


_TUPLE_FIELDS = (
    "phases",
    "system_elements",
    "aq_species_molality",
    "solid_aqueous_collisions",
    "gas_phases",
    "trace_gas_phases",
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
    aq_species_element: dict[str, str] = field(default_factory=dict)
    # Full per-element ranking {elem: [[species, element_moles], ...]}; a
    # multi-element species appears under each element it contains.
    aq_species_by_element: dict[str, list] = field(default_factory=dict)
    si: dict[str, float] = field(default_factory=dict)
    gas_si: dict[str, float] = field(default_factory=dict)
    gas_domain: dict[str, str] = field(default_factory=dict)


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
        # SYS returns total element moles as its VALUE and the species count via
        # the 2nd (by-ref) argument. Keep them in separate variables: clobbering
        # the count with the return value would break the FOR-loop bound. moles()
        # is the element's stoichiometric moles per species (sorted descending).
        lines.extend(
            [
                f"{b} et = SYS(\"{elem}\", n, nm$, ty$, mo)",
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


def _parse_species_molalities(
    row: dict, params: GridJobParams,
) -> tuple[dict[str, float], dict[str, str], dict[str, list[list]]]:
    """Merge USER_PUNCH top-species slots and explicit -mol species.

    Returns ``(out, species_elem, by_element)`` where ``out`` maps each species
    to its max element-moles (flat, for the trace ``-mol`` set), ``species_elem``
    maps a species to its first-seen element, and ``by_element`` keeps the full
    per-element ranking ``{elem: [[species, moles], ...]}``. A species containing
    several elements (e.g. ``FeHCO3+``) legitimately appears under each of them,
    each with that element's stoichiometric moles, so per-element hover/ranking is
    exact.
    """
    out: dict[str, float] = {}
    species_elem: dict[str, str] = {}
    by_element: dict[str, list[list]] = {}
    top_n = _top_aq_species_per_element(params)
    for elem in params.system_elements:
        ranked: list[list] = []
        for k in range(1, top_n + 1):
            sp = _row_str(row, f"sp_{elem}_{k}", default="")
            if not sp or sp == "none":
                continue
            m = _row_value(row, f"m_{elem}_{k}")
            if m == m and m > 0:
                out[sp] = max(out.get(sp, 0.0), m)
                species_elem.setdefault(sp, elem)
                ranked.append([sp, m])
        if ranked:
            by_element[elem] = ranked
    for sp in params.aq_species_molality:
        for key in _mol_headers(sp):
            m = _row_value(row, key)
            if m == m and m > 0:
                out[sp] = m
                break
    return out, species_elem, by_element


def _si_output_phases(params: GridJobParams) -> tuple[str, ...]:
    """Solid phases plus component trace gases for SELECTED_OUTPUT -si."""
    gases = tuple(g for g in params.trace_gas_phases if g not in ("O2(g)", "H2(g)"))
    return tuple(dict.fromkeys((*params.phases, *gases)))


def _si_from_row(row: dict, phase: str) -> float:
    return _row_value(row, f"si_{phase}", f'si_"{phase}"')


def _run_phreeqc_string(phreeqc, inp: str) -> list | None:
    """Run input; retry once when selected output is empty on first pass."""
    selected = None
    for _ in range(2):
        phreeqc.run_string(inp)
        selected = phreeqc.get_selected_output_array()
        if selected and len(selected) >= 2:
            return selected
    return selected


def _parse_grid_row(
    row: dict,
    *,
    ph: float,
    pe: float,
    params: GridJobParams,
) -> GridPointResult:
    solid_phases = {p for p in params.phases if not is_gas(p)}
    si = {phase: _si_from_row(row, phase) for phase in _si_output_phases(params)}
    gas_si = {
        g: si[g] for g in params.trace_gas_phases
        if g in si and si[g] == si[g]
    }

    from .gas_limits import water_gas_domain_labels

    gas_domain = water_gas_domain_labels(
        ph=ph,
        pe=pe,
        temp_c=params.temp_c,
        o2_limit_atm=params.o2_limit_atm,
        h2_limit_atm=params.h2_limit_atm,
    )
    for gas, val in gas_si.items():
        if gas not in ("O2(g)", "H2(g)") and val > math.log10(params.component_gas_limit_atm):
            gas_domain[gas] = f"{gas} > {params.component_gas_limit_atm:g} atm"

    dominant_aq: dict[str, str] = {}
    mol_aq: dict[str, float] = {}
    sp_mol, sp_elem, sp_by_elem = _parse_species_molalities(row, params)
    for elem in params.system_elements:
        species = _row_str(row, f"dom_{elem}", default="none")
        if species != "none":
            dominant_aq[elem] = species
        m = _row_value(row, f"m_dom_{elem}")
        if m == m:
            mol_aq[elem] = m
            if species != "none" and species not in sp_mol:
                sp_mol[species] = m
                sp_elem.setdefault(species, elem)
            # Seed the per-element ranking from the dominant when the SYS loop
            # returned nothing for this element (keeps hover non-empty).
            if species != "none" and elem not in sp_by_elem:
                sp_by_elem[elem] = [[species, m]]

    return GridPointResult(
        ph=ph,
        pe=pe,
        converged=True,
        si=si,
        gas_si=gas_si,
        gas_domain=gas_domain,
        dominant_phase=_dominant_from_si(si, include=solid_phases, default="aqueous"),
        dominant_solid=_dominant_from_si(si, include=solid_phases, default="aqueous"),
        dominant_aq_by_element=dominant_aq,
        aq_molality_by_element=mol_aq,
        aq_molality_by_species=sp_mol,
        aq_species_element=sp_elem,
        aq_species_by_element=sp_by_elem,
    )


def _format_selected_output_block(
    params: GridJobParams,
    *,
    user_punch: str,
    mol_line: str = "",
) -> str:
    si_phases = _si_output_phases(params)
    si_list = " ".join(f'"{p}"' if " " in p or "(" in p else p for p in si_phases)
    return f"""SELECTED_OUTPUT
    -reset false
    -pH true
    -pe true
    -si {si_list}
{mol_line}{user_punch}"""


def format_grid_input(
    *,
    ph: float,
    pe: float,
    params: GridJobParams,
) -> str:
    """PhreePlot-style titration: acidic seed + Fix_H+/NaOH + fixed O2 fugacity.

    The acidic seed solution (pH 1.8, benign pe) is charge-balanced with
    ``Cl ... charge`` so PHREEQC does not fail while constructing strongly
    reducing target points.  pH is then pinned with a fictitious ``Fix_H+``
    phase titrated by ``NaOH``, and redox is imposed through the gas phase
    directly as ``SI(O2(g)) = log10(fO2)`` (no ``Fix_pe``), where
    ``log10(fO2) = 4 * (pe + pH - log K_O2)`` (see ``gas_limits.log_f_o2``).
    """
    totals_lines = "\n".join(
        f"    {name:<10} {val:.12e}" for name, val in sorted(params.totals.items()) if val > 0
    )
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
    from .gas_limits import log_f_o2

    target_log_f_o2 = log_f_o2(ph=ph, pe=pe, temp_c=params.temp_c)
    return f"""
TITLE Phase diagram (titration)
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
{totals_lines}
    Cl         1.0 charge
END
USE solution 1
EQUILIBRIUM_PHASES 1
    Fix_H+ {-ph:.12g} NaOH 10
    -force_equality true
    O2(g) {target_log_f_o2:.12g} 10
    -force_equality true
END
{_format_selected_output_block(params, user_punch=user_punch, mol_line=mol_line)}END
"""


def evaluate_point(phreeqc, *, ph: float, pe: float, params: GridJobParams) -> GridPointResult:
    base = GridPointResult(ph=ph, pe=pe, converged=False)
    try:
        selected = _run_phreeqc_string(phreeqc, format_grid_input(ph=ph, pe=pe, params=params))
        if not selected or len(selected) < 2:
            return base
        headers = selected[0]
        # Titration emits a row per reaction step; the equilibrated state is last.
        data_row = selected[-1]
        row = dict(zip(headers, data_row))
        return _parse_grid_row(row, ph=ph, pe=pe, params=params)
    except Exception:
        return base


def element_from_total_key(key: str) -> str:
    """Extract element symbol from a PHREEQC total key (e.g. C(4) -> C)."""
    m = re.match(r"^([A-Z][a-z]?)", key.strip())
    return m.group(1) if m else key
