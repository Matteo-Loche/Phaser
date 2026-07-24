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
    # Native redox frame for the y-axis sweep: "pe" or "log_fo2".
    # When "log_fo2", pe_min/pe_max hold log10(fO₂) bounds (packed as ``pe`` array).
    redox_axis: str = config.REDOX_AXIS_DEFAULT
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
    layer_elements: bool = False
    solution_mode: str = config.SOLUTION_MODE_DEFAULT
    background_molality: float = 0.0
    knobs_mode: str = config.KNOBS_MODE_DEFAULT
    sweep_skip_outside_water: bool = config.SWEEP_SKIP_OUTSIDE_WATER
    # Assemblage: bare element TOT keys to PUNCH (e.g. ("Fe", "C")), not valence
    # masters — Fe(2)/Fe(3) are usually empty once solids precipitate.
    tot_keys: tuple[str, ...] = ()


_TUPLE_FIELDS = (
    "phases",
    "system_elements",
    "aq_species_molality",
    "solid_aqueous_collisions",
    "gas_phases",
    "trace_gas_phases",
    "tot_keys",
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
    if "knobs_mode" in kwargs:
        kwargs["knobs_mode"] = config.normalize_knobs_mode(kwargs.get("knobs_mode"))
    if "redox_axis" in kwargs:
        axis = str(kwargs.get("redox_axis") or config.REDOX_AXIS_DEFAULT).strip().lower()
        if axis == "eh":
            axis = config.REDOX_AXIS_PE
        if axis not in config.REDOX_AXES:
            axis = config.REDOX_AXIS_DEFAULT
        kwargs["redox_axis"] = axis
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
    # Assemblage modes only: precipitated moles per solid (empty for SI predominance).
    phase_moles: dict[str, float] = field(default_factory=dict)
    # Assemblage convenience label from moles (SI modes leave default).
    dominant_precip: str = "aqueous"
    # Assemblage: aqueous master totals from TOT("key") in mol/kgw (valence-aware).
    aq_total_by_key: dict[str, float] = field(default_factory=dict)
    knobs_level: int = 0
    synthetic_label: str | None = None


def point_key(ph: float, pe: float) -> tuple[float, float]:
    """Hashable cache key for grid coordinates."""
    return round(float(ph), 12), round(float(pe), 12)


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
    from .dummy_medium import WORKER_DEFINITIONS

    pq.run_string(WORKER_DEFINITIONS)
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


def _format_user_punch_aq_sys(
    elements: tuple[str, ...],
    *,
    top_n: int,
) -> list[str]:
    """Original predominance USER_PUNCH body: punch SYS slots by rank index.

    No ``ty$`` filter — without EQUILIBRIUM_PHASES, SYS element lists are
    aqueous-dominated and this path stays short / cheap.
    """
    lines: list[str] = []
    base = 1000
    for idx, elem in enumerate(elements):
        b = base + idx * 200
        pad = b + 80
        cont = b + 100
        # SYS returns total element moles as its VALUE and the species count via
        # the 2nd (by-ref) argument. Keep them in separate variables; overwriting
        # the count with the return value would break the FOR-loop bound. moles()
        # is the element's stoichiometric moles per species (sorted descending).
        lines.extend(
            [
                f'{b} et = SYS("{elem}", n, nm$, ty$, mo)',
                f'{b + 10} IF n > 0 THEN PUNCH nm$(1) ELSE PUNCH "none"',
                f"{b + 20} IF n > 0 THEN PUNCH mo(1) ELSE PUNCH -999",
                f"{b + 30} FOR ii = 1 TO {top_n}",
                f"{b + 40} IF ii > n THEN GOTO {pad}",
                f"{b + 50} PUNCH nm$(ii)",
                f"{b + 60} PUNCH mo(ii)",
                f"{b + 70} GOTO {cont}",
                f'{pad} PUNCH "none"',
                f"{pad + 10} PUNCH -999",
                f"{cont} NEXT ii",
            ]
        )
    return lines


def _format_user_punch_aq_only_sys(
    elements: tuple[str, ...],
    *,
    top_n: int,
) -> list[str]:
    """Assemblage USER_PUNCH body: only ``ty$ = \"aq\"`` SYS contributors.

    Under EQUILIBRIUM_PHASES, precipitated solids rank first in SYS element
    lists (``ty$ = \"equi\"``) and would pollute aqueous maps if punched raw.
    """
    lines: list[str] = []
    base = 1000
    for idx, elem in enumerate(elements):
        b = base + idx * 200
        find_next = b + 35
        after_dom = b + 40
        scan_next = b + 85
        after_scan = b + 90
        pad_loop = b + 100
        pad_done = b + 150
        lines.extend(
            [
                f'{b} et = SYS("{elem}", n, nm$, ty$, mo)',
                f'{b + 10} dom$ = "none"',
                f"{b + 15} dm = -999",
                f"{b + 20} FOR ii = 1 TO n",
                f'{b + 25} IF ty$(ii) <> "aq" THEN GOTO {find_next}',
                f"{b + 30} dom$ = nm$(ii)",
                f"{b + 32} dm = mo(ii)",
                f"{b + 34} GOTO {after_dom}",
                f"{find_next} NEXT ii",
                f"{after_dom} PUNCH dom$",
                f"{after_dom + 5} PUNCH dm",
                f"{b + 50} j = 0",
                f"{b + 55} FOR ii = 1 TO n",
                f'{b + 60} IF ty$(ii) <> "aq" THEN GOTO {scan_next}',
                f"{b + 65} j = j + 1",
                f"{b + 70} IF j > {top_n} THEN GOTO {after_scan}",
                f"{b + 75} PUNCH nm$(ii)",
                f"{b + 80} PUNCH mo(ii)",
                f"{scan_next} NEXT ii",
                f"{after_scan} REM pad unused top-N slots",
                f"{pad_loop} IF j >= {top_n} THEN GOTO {pad_done}",
                f"{pad_loop + 10} j = j + 1",
                f'{pad_loop + 20} PUNCH "none"',
                f"{pad_loop + 30} PUNCH -999",
                f"{pad_loop + 40} GOTO {pad_loop}",
                f"{pad_done} REM element {elem} done",
            ]
        )
    return lines


def _tot_heading(key: str) -> str:
    """SELECTED_OUTPUT / USER_PUNCH heading for a master total key."""
    heading = f"tot_{key}"
    if " " in key or "(" in key or ")" in key or "+" in key or "-" in key:
        return f'"{heading}"'
    return heading


def _tot_punch_expr(key: str) -> str:
    """BASIC expression punching aqueous total molality for ``key``."""
    return f'TOT("{key}")'


def tot_keys_heading_lines(tot_keys: tuple[str, ...]) -> list[str]:
    """Public helper for tests: heading tokens for ``tot_keys``."""
    return [_tot_heading(k) for k in tot_keys]


def tot_keys_punch_lines(tot_keys: tuple[str, ...], *, base_line: int = 9000) -> list[str]:
    """Public helper for tests: USER_PUNCH lines for ``TOT`` columns."""
    return [
        f"{base_line + 10 * i} PUNCH {_tot_punch_expr(key)}"
        for i, key in enumerate(tot_keys)
    ]


def _format_user_punch(
    elements: tuple[str, ...],
    *,
    top_n: int,
    equi_phases: tuple[str, ...] = (),
    tot_keys: tuple[str, ...] = (),
) -> str:
    """USER_PUNCH for aqueous SYS rankings and optional EQUI precipitated moles.

    Predominance (``equi_phases`` empty): original short SYS-by-index punch.
    Assemblage (``equi_phases`` set): aq-only SYS filter + EQUI mole columns.
    When ``tot_keys`` is set (assemblage), also punch ``TOT("master")`` columns.
    """
    headings: list[str] = []
    for elem in elements:
        headings.extend([f"dom_{elem}", f"m_dom_{elem}"])
        for k in range(1, top_n + 1):
            headings.extend([f"sp_{elem}_{k}", f"m_{elem}_{k}"])
    for phase in equi_phases:
        if " " in phase or "(" in phase or ")" in phase:
            headings.append(f'"eq_{phase}"')
        else:
            headings.append(f"eq_{phase}")
    for key in tot_keys:
        headings.append(_tot_heading(key))
    if not headings:
        return ""
    lines = ["USER_PUNCH", f"    -headings {' '.join(headings)}", "    -start"]
    if equi_phases:
        lines.extend(_format_user_punch_aq_only_sys(elements, top_n=top_n))
    else:
        lines.extend(_format_user_punch_aq_sys(elements, top_n=top_n))
    equi_base = 8000
    for i, phase in enumerate(equi_phases):
        lines.append(
            f"{equi_base + 10 * i} PUNCH EQUI({_quote_equi_arg(phase)})"
        )
    lines.extend(tot_keys_punch_lines(tot_keys, base_line=9000))
    lines.append("    -end")
    return "\n".join(lines) + "\n"


def _parse_aq_totals(row: dict, tot_keys: tuple[str, ...]) -> dict[str, float]:
    """Read ``tot_<key>`` columns into mol/kgw totals (skip non-finite / non-positive)."""
    out: dict[str, float] = {}
    for key in tot_keys:
        m = _row_value(
            row,
            f"tot_{key}",
            f'tot_"{key}"',
            f'tot_{_quote_phreeqc_name(key)}',
        )
        if m == m and m > 0.0:
            out[key] = float(m)
    return out


def _is_solid_leak_in_aq_map(
    name: str,
    *,
    solid_phases: set[str] | frozenset[str],
    collisions: set[str] | frozenset[str],
) -> bool:
    """True when ``name`` is a selected solid that is not an aq name collision.

    Collision names (e.g. ``FeO``) can be legitimate aqueous complexes; those are
    kept. Bare phase-only names (``Hematite``) that leaked from ``equi`` SYS
    rows are dropped as a parse-time safety net.
    """
    if not name or name == "none":
        return False
    if name not in solid_phases:
        return False
    if name in collisions:
        return False
    return True


def _mol_headers(species: str) -> list[str]:
    """Candidate SELECTED_OUTPUT column names for a species molality."""
    return [f"m_{species}", f"mol_{species}", f"Mola_{species}", species]


def _parse_species_molalities(
    row: dict,
    params: GridJobParams,
    *,
    solid_phases: set[str] | frozenset[str] = frozenset(),
    collisions: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, float], dict[str, str], dict[str, list[list]]]:
    """Merge USER_PUNCH top-species slots and explicit -mol species.

    Returns ``(out, species_elem, by_element)`` where ``out`` maps each species
    to its max element-moles (flat, for the trace ``-mol`` set), ``species_elem``
    maps a species to its first-seen element, and ``by_element`` keeps the full
    per-element ranking ``{elem: [[species, moles], ...]}``. A species containing
    several elements (e.g. ``FeHCO3+``) legitimately appears under each of them,
    each with that element's stoichiometric moles, so per-element hover/ranking is
    exact.

    Names that are selected solids but not aqueous name-collisions are skipped
    when ``solid_phases`` is non-empty (assemblage parse safety net only).
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
            if _is_solid_leak_in_aq_map(
                sp, solid_phases=solid_phases, collisions=collisions
            ):
                continue
            m = _row_value(row, f"m_{elem}_{k}")
            if m == m and m > 0:
                out[sp] = max(out.get(sp, 0.0), m)
                species_elem.setdefault(sp, elem)
                ranked.append([sp, m])
        if ranked:
            by_element[elem] = ranked
    for sp in params.aq_species_molality:
        if _is_solid_leak_in_aq_map(
            sp, solid_phases=solid_phases, collisions=collisions
        ):
            continue
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


def _assemblage_solid_phases(params: GridJobParams) -> tuple[str, ...]:
    """Non-gas selected solids eligible to precipitate in EQUILIBRIUM_PHASES."""
    return tuple(p for p in params.phases if not is_gas(p))


def _quote_phreeqc_name(name: str) -> str:
    """Token for EQUILIBRIUM_PHASES / SELECTED_OUTPUT ``-si`` phase lists.

    Quote only when the name contains whitespace. Parentheses are part of many
    Thermoddem phase keys (``Fe(OH)2``, ``Ferrihydrite(2L)``, …); wrapping those
    in double quotes makes IPhreeqc report ``Phase not found`` and silently
    yields SI = -999 / NaN for predominance.
    """
    if " " in name:
        return f'"{name}"'
    return name


def _quote_equi_arg(name: str) -> str:
    """EQUI() BASIC expects a quoted string (never a bare identifier)."""
    return f'"{name}"'


def _si_output_token(name: str) -> str:
    """Phase token for ``SELECTED_OUTPUT -si`` (same quoting rules as EQUI body)."""
    return _quote_phreeqc_name(name)


def assemblage_solid_lines(params: GridJobParams) -> str:
    """EQUILIBRIUM_PHASES body lines: each solid at target SI 0, initial moles 0."""
    lines = []
    for phase in _assemblage_solid_phases(params):
        lines.append(f"    {_quote_phreeqc_name(phase)} 0 0")
    return ("\n".join(lines) + "\n") if lines else ""


def _si_from_row(row: dict, phase: str) -> float:
    return _row_value(row, f"si_{phase}", f'si_"{phase}"')


def _eq_from_row(row: dict, phase: str) -> float:
    """Precipitated moles for ``phase`` from USER_PUNCH EQUI columns."""
    return _row_value(
        row,
        f"eq_{phase}",
        f'eq_"{phase}"',
        f'eq_{_quote_phreeqc_name(phase)}',
    )


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
    # Solid-leak filtering is assemblage-only (EQUI pollutes SYS). Predominance
    # keeps the original parse path — no extra set checks per species slot.
    filter_equi_leak = config.is_assemblage_mode(params.solution_mode)
    collisions = (
        frozenset(params.solid_aqueous_collisions) if filter_equi_leak else frozenset()
    )
    si = {phase: _si_from_row(row, phase) for phase in _si_output_phases(params)}
    gas_si = {
        g: si[g] for g in params.trace_gas_phases
        if g in si and si[g] == si[g]
    }

    from .gas_limits import water_gas_domain_labels_for_params, water_stability_limits_enabled

    gas_domain: dict[str, str] = {}
    if water_stability_limits_enabled(params):
        gas_domain.update(
            water_gas_domain_labels_for_params(ph=ph, y=pe, params=params)
        )
    for gas, val in gas_si.items():
        if gas not in ("O2(g)", "H2(g)") and val > math.log10(params.component_gas_limit_atm):
            gas_domain[gas] = f"{gas} > {params.component_gas_limit_atm:g} atm"

    dominant_aq: dict[str, str] = {}
    mol_aq: dict[str, float] = {}
    sp_mol, sp_elem, sp_by_elem = _parse_species_molalities(
        row,
        params,
        solid_phases=solid_phases if filter_equi_leak else frozenset(),
        collisions=collisions,
    )
    for elem in params.system_elements:
        species = _row_str(row, f"dom_{elem}", default="none")
        if filter_equi_leak and _is_solid_leak_in_aq_map(
            species, solid_phases=solid_phases, collisions=collisions
        ):
            species = "none"
        m = _row_value(row, f"m_dom_{elem}")
        if filter_equi_leak and species == "none" and elem in sp_by_elem and sp_by_elem[elem]:
            species = str(sp_by_elem[elem][0][0])
            m = float(sp_by_elem[elem][0][1])
        if species != "none":
            dominant_aq[elem] = species
        if filter_equi_leak:
            if m == m and species != "none":
                mol_aq[elem] = m
                if species not in sp_mol:
                    sp_mol[species] = m
                    sp_elem.setdefault(species, elem)
                if elem not in sp_by_elem:
                    sp_by_elem[elem] = [[species, m]]
        else:
            # Predominance: original seeding (unchanged).
            if m == m:
                mol_aq[elem] = m
                if species != "none" and species not in sp_mol:
                    sp_mol[species] = m
                    sp_elem.setdefault(species, elem)
                if species != "none" and elem not in sp_by_elem:
                    sp_by_elem[elem] = [[species, m]]

    phase_moles: dict[str, float] = {}
    dominant_precip = "aqueous"
    aq_total_by_key: dict[str, float] = {}
    if filter_equi_leak:
        from .mineral_stability import dominant_precip_label

        for phase in _assemblage_solid_phases(params):
            m = _eq_from_row(row, phase)
            phase_moles[phase] = float(m) if m == m and m > 0.0 else 0.0
        dominant_precip = dominant_precip_label(phase_moles, solid_phases)
        if params.tot_keys:
            aq_total_by_key = _parse_aq_totals(row, params.tot_keys)

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
        phase_moles=phase_moles,
        dominant_precip=dominant_precip,
        aq_total_by_key=aq_total_by_key,
    )


def _format_selected_output_block(
    params: GridJobParams,
    *,
    user_punch: str,
    mol_line: str = "",
) -> str:
    si_phases = _si_output_phases(params)
    si_list = " ".join(_si_output_token(p) for p in si_phases)
    return f"""SELECTED_OUTPUT
    -reset false
    -pH true
    -pe true
    -si {si_list}
{mol_line}{user_punch}"""


def totals_lines(params: GridJobParams) -> str:
    return "\n".join(
        f"    {name:<10} {val:.12e}"
        for name, val in sorted(params.totals.items())
        if val > 0
    )


def format_selected_output_suffix(params: GridJobParams) -> str:
    """SELECTED_OUTPUT + USER_PUNCH block and closing END for one grid point."""
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
    return f"{_format_selected_output_block(params, user_punch=user_punch, mol_line=mol_line)}END\n"


def format_assemblage_selected_output_suffix(params: GridJobParams) -> str:
    """SELECTED_OUTPUT with SI + aqueous SYS punch + EQUI precipitated moles."""
    user_punch = _format_user_punch(
        params.system_elements,
        top_n=_top_aq_species_per_element(params),
        equi_phases=_assemblage_solid_phases(params),
        tot_keys=tuple(params.tot_keys or ()),
    )
    mol_line = ""
    if params.aq_species_molality:
        mol_tokens = " ".join(
            f'"{s}"' if " " in s or "(" in s or "-" in s else s
            for s in params.aq_species_molality
        )
        mol_line = f"    -mol {mol_tokens}\n"
    return f"{_format_selected_output_block(params, user_punch=user_punch, mol_line=mol_line)}END\n"


def format_grid_input(
    *,
    ph: float,
    pe: float,
    params: GridJobParams,
    flip_charge: bool = False,
) -> str:
    """Dispatch to the input builder for ``params.solution_mode``."""
    if params.solution_mode == "dummy_titration":
        from .input_dummy_titration import format_dummy_titration_input

        return format_dummy_titration_input(
            ph=ph, pe=pe, params=params, flip_charge=flip_charge
        )
    if params.solution_mode == "assemblage_dummy_titration":
        from .input_assemblage_dummy import format_assemblage_dummy_titration_input

        return format_assemblage_dummy_titration_input(
            ph=ph, pe=pe, params=params, flip_charge=flip_charge
        )
    if params.solution_mode == "assemblage_titration":
        from .input_assemblage_titration import format_assemblage_titration_input

        return format_assemblage_titration_input(ph=ph, pe=pe, params=params)
    from .input_titration import format_titration_input

    return format_titration_input(ph=ph, pe=pe, params=params)


def _selected_output_row_index(params: GridJobParams) -> int:
    del params
    return -1


def evaluate_point(phreeqc, *, ph: float, pe: float, params: GridJobParams) -> GridPointResult:
    base = GridPointResult(ph=ph, pe=pe, converged=False)
    flip_modes = frozenset({"dummy_titration", "assemblage_dummy_titration"})
    flips = (False, True) if params.solution_mode in flip_modes else (False,)

    from .knobs import KNOBS_PROFILE_INDEX, ladder_for_mode, run_single_profile

    # Flip-retry is independent of ladder depth: charge-guess correction, not KNOBS.
    profile_flips: list[tuple[str, bool]] = []
    for prof in ladder_for_mode(params.knobs_mode):
        if prof == "robust":
            # Robust is expensive; keep the historical single-pass (no flip) behavior.
            profile_flips.append((prof, False))
        else:
            profile_flips.extend((prof, flip) for flip in flips)

    for prof, flip in profile_flips:
        if flip and params.solution_mode not in flip_modes:
            continue
        try:
            inp = format_grid_input(ph=ph, pe=pe, params=params, flip_charge=flip)
            selected = run_single_profile(phreeqc, prof, inp)
            if not selected or len(selected) < 2:
                continue
            headers = selected[0]
            data_row = selected[_selected_output_row_index(params)]
            row = dict(zip(headers, data_row))
            result = _parse_grid_row(row, ph=ph, pe=pe, params=params)
            result.knobs_level = KNOBS_PROFILE_INDEX.get(prof, 0)
            return result
        except Exception:
            continue
    return base


def element_from_total_key(key: str) -> str:
    """Extract element symbol from a PHREEQC total key (e.g. C(4) -> C)."""
    m = re.match(r"^([A-Z][a-z]?)", key.strip())
    return m.group(1) if m else key
