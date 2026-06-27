"""Pack raw PHREEQC grid results into layered predominance diagrams."""
from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np

from ..phreeqc.engine import GridJobParams
from .phases import phase_element_map


def subset_key(subset: tuple[str, ...]) -> str:
    return "-".join(sorted(subset))


def subsets_to_pack(elements: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Non-empty element subsets for solid predominance layers."""
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


def dominant_aq_in_subset(row: dict, subset: set[str]) -> str:
    aq = row.get("dominant_aq_by_element") or {}
    mols = row.get("aq_molality_by_element") or {}
    best_species = "none"
    best_m = -1.0
    for elem in subset:
        sp = aq.get(elem)
        if not sp or sp == "none":
            continue
        m = mols.get(elem, 0.0)
        if m > best_m:
            best_m = m
            best_species = sp
    return best_species


def category_solid_subset(
    row: dict,
    subset: tuple[str, ...],
    *,
    phase_elements: dict[str, frozenset[str]],
    job_phases: tuple[str, ...],
) -> str:
    from ..db.parser import is_gas

    subset_set = set(subset)
    eligible = {
        p for p in job_phases
        if not is_gas(p)
        and phase_elements.get(p, frozenset()).issubset(subset_set)
    }
    si = row.get("si") or {}
    finite = {p: si[p] for p in eligible if p in si and si[p] == si[p]}
    if finite:
        phase, value = max(finite.items(), key=lambda kv: kv[1])
        if value >= 0.0:
            return phase
    return dominant_aq_in_subset(row, subset_set)


def pack_category_grid(
    rows: list[dict],
    *,
    field: str,
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    pe_levels: int,
    ph_levels: int,
    element_key: str | None = None,
) -> dict[str, Any]:
    categories: set[str] = set()
    for row in rows:
        if element_key:
            val = row.get("dominant_aq_by_element", {}).get(element_key, "none")
        else:
            val = row.get(field, "none")
        categories.add(val if val else "none")

    names = sorted(categories)
    index = {name: i for i, name in enumerate(names)}
    grid = np.full((pe_levels, ph_levels), -1, dtype=int)

    for row in rows:
        ix = ph_lookup.get(round(float(row["ph"]), 12))
        iy = pe_lookup.get(round(float(row["pe"]), 12))
        if ix is None or iy is None:
            continue
        if element_key:
            cat = row.get("dominant_aq_by_element", {}).get(element_key, "none")
        else:
            cat = row.get(field, "none")
        cat = cat if cat else "none"
        grid[iy, ix] = index.get(cat, -1)

    return {"names": names, "grid": grid.tolist()}


def pack_subset_grid(
    rows: list[dict],
    *,
    subset: tuple[str, ...],
    phase_elements: dict[str, frozenset[str]],
    job_phases: tuple[str, ...],
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    pe_levels: int,
    ph_levels: int,
) -> dict[str, Any]:
    categories: set[str] = set()
    for row in rows:
        categories.add(
            category_solid_subset(
                row, subset, phase_elements=phase_elements, job_phases=job_phases
            )
        )
    names = sorted(categories)
    index = {name: i for i, name in enumerate(names)}
    grid = np.full((pe_levels, ph_levels), -1, dtype=int)
    for row in rows:
        ix = ph_lookup.get(round(float(row["ph"]), 12))
        iy = pe_lookup.get(round(float(row["pe"]), 12))
        if ix is None or iy is None:
            continue
        cat = category_solid_subset(
            row, subset, phase_elements=phase_elements, job_phases=job_phases
        )
        grid[iy, ix] = index.get(cat, -1)
    solid_set = set(job_phases)
    aqueous_names = [n for n in names if n not in solid_set]
    return {
        "names": names,
        "grid": grid.tolist(),
        "elements": list(subset),
        "aqueous_names": aqueous_names,
    }


def pack_grid_results(
    params: GridJobParams,
    rows: list[dict],
    *,
    db_path: str,
) -> dict[str, Any]:
    ph = np.linspace(params.ph_min, params.ph_max, params.ph_levels)
    pe = np.linspace(params.pe_min, params.pe_max, params.pe_levels)

    ph_lookup = {round(float(value), 12): i for i, value in enumerate(ph)}
    pe_lookup = {round(float(value), 12): i for i, value in enumerate(pe)}

    phase_elements = phase_element_map(db_path)
    solid_subsets: dict[str, Any] = {}
    for subset in subsets_to_pack(params.system_elements):
        key = subset_key(subset)
        solid_subsets[key] = pack_subset_grid(
            rows,
            subset=subset,
            phase_elements=phase_elements,
            job_phases=params.phases,
            ph_lookup=ph_lookup,
            pe_lookup=pe_lookup,
            pe_levels=params.pe_levels,
            ph_levels=params.ph_levels,
        )

    layers: dict[str, Any] = {
        "solid_subsets": solid_subsets,
        "elements": {},
    }
    for elem in params.system_elements:
        layers["elements"][elem] = pack_category_grid(
            rows,
            field="",
            element_key=elem,
            ph_lookup=ph_lookup,
            pe_lookup=pe_lookup,
            pe_levels=params.pe_levels,
            ph_levels=params.ph_levels,
        )

    default_key = subset_key(params.system_elements)
    default_layer = solid_subsets.get(default_key) or next(
        iter(solid_subsets.values()), {"names": [], "grid": []}
    )

    return {
        "ph": ph.tolist(),
        "pe": pe.tolist(),
        "system_elements": list(params.system_elements),
        "layers": layers,
        "phase_names": default_layer["names"],
        "grid": default_layer["grid"],
        "n_converged": sum(1 for r in rows if r.get("converged")),
        "n_total": len(rows),
        "temp_c": params.temp_c,
    }
