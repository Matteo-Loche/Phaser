"""PHREEQC SYS-based database catalog scanning."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from .. import config

MAX_SLOTS = 48
_DELIM = "|"
# Bump when catalog parsing or stored fields change (invalidates cached SQLite catalogs).
SCHEMA_VERSION = 5

# Element extraction from PHREEQC phase formulae (PHASES block). O/H/charge are
# dropped because every aqueous system already contains water; subset
# eligibility is driven by the "interesting" elements only.
_ELEMENT_IN_SPECIES = re.compile(r"([A-Z][a-z]?)[+-]")
_FORMULA_ELEMENTS = re.compile(r"([A-Z][a-z]?)(?=\d|[A-Z]|\(|\)|\+|-|\s|$)")
_NON_ELEMENTS = {"O", "H", "E", "e"}

# Top-level PHREEQC datablock keywords. Used to bound the PHASES block: a
# database may place PITZER/SIT/EXCHANGE_* blocks after PHASES, and parsing into
# them produces junk "phase" names from interaction-parameter lines.
_DB_KEYWORDS = frozenset({
    "SOLUTION_MASTER_SPECIES", "SOLUTION_SPECIES", "PHASES",
    "EXCHANGE_MASTER_SPECIES", "EXCHANGE_SPECIES",
    "SURFACE_MASTER_SPECIES", "SURFACE_SPECIES",
    "RATES", "PITZER", "SIT", "ISOTOPES", "ISOTOPE_RATIOS",
    "ISOTOPE_ALPHAS", "CALCULATE_VALUES", "NAMED_EXPRESSIONS",
    "LLNL_AQUEOUS_MODEL_PARAMETERS", "SOLID_SOLUTIONS",
    "EXCHANGE", "SURFACE", "GAS_PHASE", "END", "DATABASE",
    "SELECTED_OUTPUT", "USER_PUNCH", "KNOBS",
})
# Phase sub-option lines that must never be treated as phase names.
_PHASE_OPTION_PREFIXES = (
    "log_k", "logk", "delta_h", "deltah", "-", "analytic", "vm ", "vm\t",
    "t_c", "p_c", "omega", "gas_comp", "add_logk", "add_constant",
)
# When the default probe amount fails to converge, retry at lower multiples of
# CATALOG_PROBE_AMOUNT (same units). Membership from SYS does not depend on
# amount; only convergence does.
_PROBE_FALLBACK_SCALES = (1.0, 1e-2, 1e-4, 1e-6)


def is_gas(name: str) -> bool:
    return name.strip().lower().endswith("(g)")


@dataclass(frozen=True)
class PhaseProbeHit:
    name: str
    si: float


@dataclass(frozen=True)
class PhaseProbeResult:
    count: int
    phases: tuple[PhaseProbeHit, ...]


@dataclass(frozen=True)
class ElementProbeHit:
    name: str
    kind: str


@dataclass(frozen=True)
class ElementProbeResult:
    count: int
    entries: tuple[ElementProbeHit, ...]


@dataclass(frozen=True)
class DatabaseCatalogSnapshot:
    db_path: str
    accepted_totals: tuple[str, ...]
    elements: tuple[ElementProbeHit, ...]
    solid_phases: tuple[PhaseProbeHit, ...]
    gas_phases: tuple[PhaseProbeHit, ...]
    species_by_element: dict[str, tuple[str, ...]]
    solid_aqueous_collisions: frozenset[str]
    # phase name -> sorted tuple of constituent elements (excluding O/H/charge),
    # parsed from the database PHASES block. Drives element-subset eligibility.
    phase_elements: dict[str, tuple[str, ...]] = field(default_factory=dict)
    phase_count: int = 0
    species_count: int = 0


def element_from_total_key(key: str) -> str:
    m = re.match(r"^([A-Z][a-z]?)", key.strip())
    return m.group(1) if m else key


def subset_key(subset: tuple[str, ...]) -> str:
    return "-".join(sorted(subset))


def subsets_for_scan(elements: tuple[str, ...]) -> list[tuple[str, ...]]:
    elems = sorted(elements)
    n = len(elems)
    if n == 0:
        return []
    if n <= 7:
        out: list[tuple[str, ...]] = []
        for r in range(1, n + 1):
            for combo in combinations(elems, r):
                out.append(combo)
        return out
    out = [(e,) for e in elems]
    out.extend(combinations(elems, 2))
    out.append(tuple(elems))
    return out


def element_symbols_from_totals(totals: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({element_from_total_key(t) for t in totals}))


def coalesce_totals_per_element(accepted: tuple[str, ...]) -> tuple[str, ...]:
    """Keep one PHREEQC total key per element for joint probe solutions."""
    by_symbol: dict[str, list[str]] = {}
    for key in accepted:
        by_symbol.setdefault(element_from_total_key(key), []).append(key)
    out: list[str] = []
    for sym in sorted(by_symbol):
        keys = by_symbol[sym]
        with_redox = [k for k in keys if "(" in k]
        out.append(with_redox[0] if with_redox else keys[0])
    return tuple(out)


def probe_totals_dict(
    accepted: tuple[str, ...],
    *,
    amount: float = config.CATALOG_PROBE_AMOUNT,
    element_filter: set[str] | None = None,
) -> dict[str, float]:
    keys = coalesce_totals_per_element(accepted)
    if element_filter is not None:
        keys = tuple(k for k in keys if element_from_total_key(k) in element_filter)
    return {key: amount for key in keys}


def _totals_lines(totals: dict[str, float]) -> str:
    return "\n".join(
        f"    {name:<10} {val:.12e}" for name, val in sorted(totals.items()) if val > 0
    )


def _delimited_phase_punch_block() -> str:
    return "\n".join(
        [
            "USER_PUNCH",
            "    -headings n_phases phase_blob",
            "    -start",
            "10 n = SYS(\"phases\", count, name$, type$, si_vals)",
            "20 blob$ = \"\"",
            "30 FOR i = 1 TO count",
            f"40   IF i > 1 THEN blob$ = blob$ + \"{_DELIM}\"",
            "50   blob$ = blob$ + name$(i) + \" \" + STR_F$(si_vals(i), 16, 6)",
            "60 NEXT i",
            "70 PUNCH count, blob$",
            "    -end",
        ]
    ) + "\n"


def _delimited_element_punch_block() -> str:
    return "\n".join(
        [
            "USER_PUNCH",
            "    -headings n_elements element_blob",
            "    -start",
            "10 n = SYS(\"elements\", count, name$, type$, moles)",
            "20 blob$ = \"\"",
            "30 FOR i = 1 TO count",
            f"40   IF i > 1 THEN blob$ = blob$ + \"{_DELIM}\"",
            "50   blob$ = blob$ + name$(i) + \" \" + type$(i)",
            "60 NEXT i",
            "70 PUNCH count, blob$",
            "    -end",
        ]
    ) + "\n"


def _delimited_aqueous_punch_block() -> str:
    return "\n".join(
        [
            "USER_PUNCH",
            "    -headings n_aq aq_blob",
            "    -start",
            "10 n = SYS(\"aq\", count, nm$, ty$, mo)",
            "20 blob$ = \"\"",
            "30 FOR i = 1 TO count",
            f"40   IF i > 1 THEN blob$ = blob$ + \"{_DELIM}\"",
            "50   blob$ = blob$ + nm$(i)",
            "60 NEXT i",
            "70 PUNCH count, blob$",
            "    -end",
        ]
    ) + "\n"


def _delimited_species_punch_block(element: str) -> str:
    return "\n".join(
        [
            "USER_PUNCH",
            "    -headings n_species species_blob",
            "    -start",
            f"10 n = SYS(\"{element}\", count, nm$, ty$, mo)",
            "20 blob$ = \"\"",
            "30 FOR i = 1 TO count",
            f"40   IF i > 1 THEN blob$ = blob$ + \"{_DELIM}\"",
            "50   blob$ = blob$ + nm$(i) + \" \" + ty$(i)",
            "60 NEXT i",
            "70 PUNCH count, blob$",
            "    -end",
        ]
    ) + "\n"


def build_probe_input(
    *,
    totals: dict[str, float],
    punch_block: str,
    units: str = config.DEFAULT_UNITS,
    temp_c: float = 25.0,
    ph: float = 7.0,
    pe: float = 4.0,
) -> str:
    totals_block = _totals_lines(totals)
    return f"""
TITLE PHASER catalog probe
SOLUTION 1
    temp      {temp_c:.6g}
    units     {units}
    water     {config.WATER_MASS_KGW:.12e}
    pH        {ph:.12g}
    pe        {pe:.12g}
{totals_block}

SELECTED_OUTPUT
    -reset false
{punch_block}END
"""


def selected_dict(pq) -> dict[str, Any]:
    selected = pq.get_selected_output_array()
    if not selected or len(selected) < 2:
        return {}
    headers = [str(h).strip() for h in selected[0]]
    return dict(zip(headers, selected[1]))


def _split_blob_pair(part: str) -> tuple[str, str]:
    part = part.strip()
    if not part:
        return "", ""
    bits = part.rsplit(None, 1)
    if len(bits) == 2:
        return bits[0].strip(), bits[1].strip()
    return part, ""


def _parse_delimited_phases(blob: str) -> list[PhaseProbeHit]:
    hits: list[PhaseProbeHit] = []
    text = str(blob or "").strip()
    for part in text.split(_DELIM):
        name, si_s = _split_blob_pair(part)
        if not name:
            continue
        try:
            si = float(si_s)
        except (TypeError, ValueError):
            si = float("nan")
        hits.append(PhaseProbeHit(name=name, si=si))
    return hits


def _parse_delimited_elements(blob: str) -> list[ElementProbeHit]:
    hits: list[ElementProbeHit] = []
    for part in str(blob or "").strip().split(_DELIM):
        name, kind = _split_blob_pair(part)
        if name:
            hits.append(ElementProbeHit(name=name, kind=kind))
    return hits


def _parse_delimited_species(blob: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for part in str(blob or "").strip().split(_DELIM):
        name, kind = _split_blob_pair(part)
        if name:
            hits.append((name, kind))
    return hits


def run_phases_delimited_probe(
    pq,
    *,
    totals: dict[str, float],
    units: str = config.DEFAULT_UNITS,
) -> PhaseProbeResult:
    inp = build_probe_input(
        totals=totals,
        punch_block=_delimited_phase_punch_block(),
        units=units,
    )
    pq.run_string(inp)
    row = selected_dict(pq)
    count = int(float(row.get("n_phases", 0) or 0))
    phases = tuple(_parse_delimited_phases(str(row.get("phase_blob", "") or "")))
    return PhaseProbeResult(count=count, phases=phases)


# Probe solutions can fail to converge for some databases/element mixes.
# Retry at lower multiples of the configured probe amount before giving up.


def converging_phases_probe(
    pq,
    *,
    totals: dict[str, float],
    units: str = config.DEFAULT_UNITS,
) -> tuple[PhaseProbeResult, dict[str, float]]:
    """Run the phases probe, retrying at lower molality until it converges.

    Returns the result and the (possibly scaled) totals that converged, so the
    same concentration can be reused for the element/species probes.
    """
    if not totals:
        return run_phases_delimited_probe(pq, totals=totals, units=units), totals
    last_exc: Exception | None = None
    for scale in _PROBE_FALLBACK_SCALES:
        scaled = {k: v * scale for k, v in totals.items()}
        try:
            return run_phases_delimited_probe(pq, totals=scaled, units=units), scaled
        except Exception as exc:  # PHREEQC convergence/input error
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def run_elements_delimited_probe(
    pq,
    *,
    totals: dict[str, float],
    units: str = config.DEFAULT_UNITS,
) -> ElementProbeResult:
    inp = build_probe_input(
        totals=totals,
        punch_block=_delimited_element_punch_block(),
        units=units,
    )
    pq.run_string(inp)
    row = selected_dict(pq)
    count = int(float(row.get("n_elements", 0) or 0))
    entries = tuple(_parse_delimited_elements(str(row.get("element_blob", "") or "")))
    return ElementProbeResult(count=count, entries=entries)


def run_aqueous_species_probe(
    pq,
    *,
    totals: dict[str, float],
    units: str = config.DEFAULT_UNITS,
) -> tuple[str, ...]:
    """All aqueous species for the solution in ONE equilibration via SYS("aq").

    Far cheaper than probing each element separately, which re-equilibrates the
    full (often large) solution once per element.
    """
    inp = build_probe_input(
        totals=totals,
        punch_block=_delimited_aqueous_punch_block(),
        units=units,
    )
    pq.run_string(inp)
    row = selected_dict(pq)
    blob = str(row.get("aq_blob", "") or "")
    names = [s.strip() for s in blob.split(_DELIM) if s.strip()]
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return tuple(seen)


def group_species_by_element(
    species: tuple[str, ...],
    symbols: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Bucket aqueous species under each accepted element symbol they contain."""
    symbol_set = set(symbols)
    buckets: dict[str, list[str]] = {s: [] for s in symbols}
    for name in species:
        for elem in extract_formula_elements(name):
            if elem in symbol_set:
                buckets[elem].append(name)
    return {sym: tuple(names) for sym, names in buckets.items()}


def run_species_delimited_probe(
    pq,
    element: str,
    *,
    totals: dict[str, float],
    units: str = config.DEFAULT_UNITS,
) -> tuple[int, tuple[str, ...]]:
    inp = build_probe_input(
        totals=totals,
        punch_block=_delimited_species_punch_block(element),
        units=units,
    )
    pq.run_string(inp)
    row = selected_dict(pq)
    count = int(float(row.get("n_species", 0) or 0))
    hits = _parse_delimited_species(str(row.get("species_blob", "") or ""))
    return count, tuple(name for name, _kind in hits)


def open_phreeqc(db_path: str, dll_path: str):
    from phreeqpy.iphreeqc import phreeqc_dll as phreeqc_dll_mod

    pq = phreeqc_dll_mod.IPhreeqc(dll_path=str(dll_path))
    pq.load_database(str(db_path))
    err = str(pq.get_error_string() or "").strip()
    if err:
        raise RuntimeError(f"Failed to load database {db_path}:\n{err}")
    return pq


def probe_total_accepted(
    pq,
    total_key: str,
    *,
    units: str = config.DEFAULT_UNITS,
    amount: float = config.CATALOG_PROBE_AMOUNT,
) -> bool:
    symbol = element_from_total_key(total_key)
    try:
        count, names = run_species_delimited_probe(
            pq,
            symbol,
            totals={total_key: amount},
            units=units,
        )
        return count > 0 and len(names) > 0
    except Exception:
        return False


def probe_accepted_totals(
    pq,
    candidates: tuple[str, ...] = config.CATALOG_TOTAL_CANDIDATES,
    *,
    units: str = config.DEFAULT_UNITS,
    amount: float = config.CATALOG_PROBE_AMOUNT,
) -> tuple[str, ...]:
    accepted: list[str] = []
    for key in candidates:
        if probe_total_accepted(pq, key, units=units, amount=amount):
            accepted.append(key)
    return tuple(accepted)


def detect_solid_aqueous_collisions(
    phases: tuple[PhaseProbeHit, ...],
    species_by_element: dict[str, tuple[str, ...]],
) -> frozenset[str]:
    phase_names = {p.name for p in phases}
    species_names = {s for names in species_by_element.values() for s in names}
    return frozenset(phase_names & species_names)


def extract_formula_elements(text: str) -> frozenset[str]:
    """Constituent elements in a PHREEQC formula/reaction fragment (no O/H/charge)."""
    elems = set(_ELEMENT_IN_SPECIES.findall(text))
    elems.update(_FORMULA_ELEMENTS.findall(text))
    return frozenset(e for e in elems if e not in _NON_ELEMENTS)


def parse_phase_elements(db_path: str) -> dict[str, frozenset[str]]:
    """Map every PHASES-block phase name to its constituent elements.

    Element composition is not exposed by PHREEQC's SYS interface, so it is read
    from the database PHASES block once. Used to compute element-subset
    eligibility without per-subset PHREEQC probing.

    Robust against:
      * other datablocks after PHASES (PITZER/SIT/EXCHANGE_*), which are skipped
        via top-level keyword tracking rather than fixed end markers;
      * trailing index numbers on the name line (e.g. ``Brucite 19``), by taking
        the name as the first whitespace token;
      * label suffixes like ``Ferrihydrite(2L)``, by reading composition from
        the reaction rather than the name.
    """
    from pathlib import Path

    lines = Path(db_path).read_text(encoding="utf-8", errors="replace").splitlines()
    out: dict[str, frozenset[str]] = {}
    in_phases = False
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        # Top-level datablock keyword (starts at column 0) toggles the block.
        if raw[:1] not in (" ", "\t"):
            head = stripped.split()[0].split("#")[0].upper()
            if head in _DB_KEYWORDS:
                in_phases = head == "PHASES"
                continue
        if not in_phases:
            continue
        low = stripped.lower()
        # Reaction lines (have "=") are consumed via the name's lookahead;
        # option lines (log_k, -analytic, delta_h, -Vm, ...) are not names.
        if "=" in stripped or low.startswith(_PHASE_OPTION_PREFIXES):
            continue
        # Phase name = first whitespace token (drops trailing index numbers).
        name = stripped.split()[0]
        # The reaction is the next meaningful line and must contain "=".
        j = i
        while j < n and (not lines[j].strip() or lines[j].strip().startswith("#")):
            j += 1
        if j >= n or "=" not in lines[j]:
            continue
        reaction = lines[j].strip()
        i = j + 1
        # Composition from the reaction (carries the formula), never the name.
        elems = extract_formula_elements(reaction)
        if name and name not in out:
            out[name] = elems
    return out


def scan_database_catalog(
    pq,
    db_path: str,
    *,
    total_candidates: tuple[str, ...] | None = None,
    known_totals: tuple[str, ...] | None = None,
    units: str = config.DEFAULT_UNITS,
    amount: float = config.CATALOG_PROBE_AMOUNT,
) -> DatabaseCatalogSnapshot:
    candidates = (
        known_totals
        or total_candidates
        or config.CATALOG_TOTAL_CANDIDATES
    )
    accepted = probe_accepted_totals(pq, candidates, units=units, amount=amount)
    probe_totals = probe_totals_dict(accepted, amount=amount)
    # Equilibrate once at a converging concentration so SYS("aq") (species) and
    # best-effort SI values are available. The probe's temperature/pH/pe are NOT
    # used to decide which phases exist (see below).
    baseline_phases, probe_totals = converging_phases_probe(
        pq, totals=probe_totals, units=units
    )
    elements = run_elements_delimited_probe(pq, totals=probe_totals, units=units)

    # One equilibration for every aqueous species, then group by element via
    # formula. Probing each element separately re-equilibrates the full solution
    # per element and stalls on large databases (e.g. sit.dat, llnl.dat).
    symbols = element_symbols_from_totals(accepted)
    all_species = run_aqueous_species_probe(pq, totals=probe_totals, units=units)
    species_by_element = group_species_by_element(all_species, symbols)
    species_count = len(all_species)

    # The phase catalog (names, kind, element composition) comes from the
    # database PHASES block, which is complete and independent of the probe's
    # temperature / pH / pe. SYS("phases") only reports phases whose redox state
    # matches the probe solution, so e.g. Fe(III) oxides/oxyhydroxides (Hematite,
    # Goethite, Magnetite, ...) are dropped at a reducing pe. The probe is used
    # only for best-effort SI metadata.
    formula_map = parse_phase_elements(db_path)
    si_by_name = {p.name: p.si for p in baseline_phases.phases}

    solids: list[PhaseProbeHit] = []
    gases: list[PhaseProbeHit] = []
    phase_elements: dict[str, tuple[str, ...]] = {}
    for name, elems in formula_map.items():
        if "(element)" in name.lower():
            continue
        si = si_by_name.get(name, float("nan"))
        if is_gas(name):
            gases.append(PhaseProbeHit(name=name, si=si))
            continue
        if not elems:
            continue
        solids.append(PhaseProbeHit(name=name, si=si))
        phase_elements[name] = tuple(sorted(elems))

    phase_names = {p.name for p in solids} | {p.name for p in gases}
    collisions = frozenset(phase_names & set(all_species))

    return DatabaseCatalogSnapshot(
        db_path=db_path,
        accepted_totals=accepted,
        elements=elements.entries,
        solid_phases=tuple(solids),
        gas_phases=tuple(gases),
        species_by_element=species_by_element,
        solid_aqueous_collisions=collisions,
        phase_elements=phase_elements,
        phase_count=len(solids) + len(gases),
        species_count=species_count,
    )
