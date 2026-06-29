"""Pack raw PHREEQC grid results into layered predominance diagrams.

Category labels for solid-subset layers come from ``category_solid_subset``. When
a phase name is also reported as an aqueous species, the precipitated solid is
labelled ``<name>(s)`` so it stays distinct from the aqueous complex (see
``solid_aqueous_collisions`` and ``solid_label``).
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Callable

import numpy as np

from ..phreeqc.engine import GridJobParams
from ..phreeqc.catalog import is_gas


SOLID_SUFFIX = "(s)"


def solid_label(phase: str, collision_names: frozenset[str]) -> str:
    """Display label for a precipitated solid.

    When a phase name is also used by an aqueous species (e.g. ``FeO``), the
    solid is written ``FeO(s)`` so it stays a distinct category from the aqueous
    complex of the same name. Non-colliding solids keep their bare phase name.
    """
    return f"{phase}{SOLID_SUFFIX}" if phase in collision_names else phase


def phase_from_label(label: str) -> str:
    """Phase name behind a category label (strips the solid ``(s)`` suffix)."""
    return label[: -len(SOLID_SUFFIX)] if label.endswith(SOLID_SUFFIX) else label


def label_is_solid(
    label: str,
    solid_set: set[str] | frozenset[str],
    collision_names: frozenset[str],
) -> bool:
    """Whether a category label denotes a precipitated solid (not an aqueous complex).

    Structural, not a heuristic: a solid is either ``<phase>(s)`` (a colliding
    name) or a bare phase name that is not a collision. A bare colliding name
    therefore always means the aqueous species.
    """
    if label.endswith(SOLID_SUFFIX) and label[: -len(SOLID_SUFFIX)] in solid_set:
        return True
    return label in solid_set and label not in collision_names


def solid_aqueous_collisions(rows: list[dict], job_phases: tuple[str, ...]) -> frozenset[str]:
    """Phase names that also occur as aqueous species names across the results.

    Derived purely from the job's phase list and the species PHREEQC reports, so
    it is database- and system-agnostic (no hardcoded names).
    """
    solid_set = set(job_phases)
    aq_names: set[str] = set()
    for row in rows:
        for sp in (row.get("dominant_aq_by_element") or {}).values():
            if sp and sp not in ("none", "aqueous"):
                aq_names.add(sp)
        aq_names.update((row.get("aq_molality_by_species") or {}).keys())
    return frozenset(solid_set & aq_names)


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
    eligible_phases: frozenset[str],
    job_phases: tuple[str, ...],
    collision_names: frozenset[str] = frozenset(),
) -> str:
    eligible = {p for p in job_phases if p in eligible_phases and not is_gas(p)}
    si = row.get("si") or {}
    finite = {p: si[p] for p in eligible if p in si and si[p] == si[p]}
    if finite:
        phase, value = max(finite.items(), key=lambda kv: kv[1])
        if value >= 0.0:
            return solid_label(phase, collision_names)
    return dominant_aq_in_subset(row, set(subset))


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
    eligible_phases: frozenset[str],
    job_phases: tuple[str, ...],
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    pe_levels: int,
    ph_levels: int,
    collision_names: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    categories: set[str] = set()
    for row in rows:
        categories.add(
            category_solid_subset(
                row, subset, eligible_phases=eligible_phases,
                job_phases=job_phases, collision_names=collision_names,
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
            row, subset, eligible_phases=eligible_phases,
            job_phases=job_phases, collision_names=collision_names,
        )
        grid[iy, ix] = index.get(cat, -1)
    solid_set = set(job_phases)
    aqueous_names = [n for n in names if not label_is_solid(n, solid_set, collision_names)]
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
    db_path: str | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    del db_path
    ph = np.linspace(params.ph_min, params.ph_max, params.ph_levels)
    pe = np.linspace(params.pe_min, params.pe_max, params.pe_levels)

    ph_lookup = {round(float(value), 12): i for i, value in enumerate(ph)}
    pe_lookup = {round(float(value), 12): i for i, value in enumerate(pe)}

    subset_list = subsets_to_pack(params.system_elements)
    pack_steps = len(subset_list) + len(params.system_elements)
    step = 0

    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset

    def tick() -> None:
        nonlocal step
        step += 1
        if progress_cb:
            progress_cb(step, pack_steps)

    solid_subsets: dict[str, Any] = {}
    for subset in subset_list:
        key = subset_key(subset)
        eligible = frozenset(subset_map.get(key, ()))
        solid_subsets[key] = pack_subset_grid(
            rows,
            subset=subset,
            eligible_phases=eligible,
            job_phases=params.phases,
            ph_lookup=ph_lookup,
            pe_lookup=pe_lookup,
            pe_levels=params.pe_levels,
            ph_levels=params.ph_levels,
            collision_names=collisions,
        )
        tick()

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
        tick()

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
