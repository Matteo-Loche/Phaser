"""Pack raw PHREEQC grid results into layered predominance / mineral-stability diagrams.

Saturation predominance uses ``category_solid_subset`` (max SI). Mineral-stability
assemblage jobs use ``pack_mineral_grid_results`` with precipitated-mole categories
(``moles`` or ``costability``). When a phase name is also an aqueous species, the
precipitated solid is labelled ``<name>(s)`` (see ``solid_aqueous_collisions``).
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Callable, Literal

import numpy as np

from .. import config
from ..phreeqc.engine import GridJobParams
from ..phreeqc.catalog import is_gas
from ..phreeqc.dummy_medium import EXCLUDED_SPECIES


SOLID_SUFFIX = "(s)"
HOVER_SPECIES_PER_ELEMENT = config.HOVER_SPECIES_PER_ELEMENT


def top_species_entries(row: dict, *, per_element: int = HOVER_SPECIES_PER_ELEMENT) -> list[list]:
    """Top species per element as ``[name, element_moles, element]`` for a point.

    Keeps the top ``per_element`` species for *each* element so the client can
    filter to any element subset and still rank correctly. A species containing
    several elements (e.g. ``FeHCO3+``) emits one entry per element it contains,
    each tagged with that element's moles, so an element-filtered hover shows it;
    the client de-duplicates by name after filtering. Sorted by moles descending.
    """
    if not row.get("converged"):
        return []
    by_elem: dict[str, list] = row.get("aq_species_by_element") or {}
    if by_elem:
        entries: list[list] = []
        for elem, lst in by_elem.items():
            ranked = sorted(
                ([sp, m] for sp, m in lst if m == m and m > 0),
                key=lambda kv: kv[1], reverse=True,
            )
            for sp, m in ranked[:per_element]:
                if sp in EXCLUDED_SPECIES:
                    continue
                entries.append([sp, m, elem])
        entries.sort(key=lambda e: e[1], reverse=True)
        return entries

    # Fallback when per-element rankings are absent: use flat species maps.
    mols: dict[str, float] = dict(row.get("aq_molality_by_species") or {})
    elem_map: dict[str, str] = dict(row.get("aq_species_element") or {})
    if not mols:
        for elem, sp in (row.get("dominant_aq_by_element") or {}).items():
            if not sp or sp == "none":
                continue
            m = (row.get("aq_molality_by_element") or {}).get(elem)
            if m is not None and m == m and m > 0:
                mols.setdefault(sp, m)
                elem_map.setdefault(sp, elem)
    if not mols:
        return []
    flat_by_elem: dict[str, list[tuple[str, float]]] = {}
    for sp, m in mols.items():
        if not (m == m) or m <= 0:
            continue
        flat_by_elem.setdefault(elem_map.get(sp, ""), []).append((sp, m))
    entries = []
    for elem, lst in flat_by_elem.items():
        lst.sort(key=lambda kv: kv[1], reverse=True)
        for sp, m in lst[:per_element]:
            entries.append([sp, m, elem])
    entries.sort(key=lambda e: e[1], reverse=True)
    return entries


def pack_hover_species_grid(
    rows: list[dict],
    *,
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    pe_levels: int,
    ph_levels: int,
) -> list[list[list]]:
    grid: list[list[list]] = [[[] for _ in range(ph_levels)] for _ in range(pe_levels)]
    for row in rows:
        ix = ph_lookup.get(round(float(row["ph"]), 12))
        iy = pe_lookup.get(round(float(row["pe"]), 12))
        if ix is None or iy is None:
            continue
        grid[iy][ix] = top_species_entries(row)
    return grid


def precip_hover_entries(
    row: dict,
    *,
    collision_names: frozenset[str] = frozenset(),
    eps: float = 1e-16,
) -> list[list]:
    """Non-zero precipitated solids as ``[label, moles]`` for hover (moles desc)."""
    raw = row.get("phase_moles") or {}
    if not raw or not row.get("converged"):
        return []
    out: list[list] = []
    for name, moles in raw.items():
        if moles != moles:
            continue
        m = float(moles)
        if m <= eps:
            continue
        if is_gas(str(name)):
            continue
        out.append([solid_label(str(name), collision_names), m])
    out.sort(key=lambda e: e[1], reverse=True)
    return out


def pack_hover_precip_grid(
    rows: list[dict],
    *,
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    pe_levels: int,
    ph_levels: int,
    collision_names: frozenset[str] = frozenset(),
) -> list[list[list]]:
    """Per-cell list of precipitated solids with moles > 0 (assemblage hover)."""
    grid: list[list[list]] = [[[] for _ in range(ph_levels)] for _ in range(pe_levels)]
    for row in rows:
        ix = ph_lookup.get(round(float(row["ph"]), 12))
        iy = pe_lookup.get(round(float(row["pe"]), 12))
        if ix is None or iy is None:
            continue
        grid[iy][ix] = precip_hover_entries(row, collision_names=collision_names)
    return grid


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
    name), a co-stability / moles-tie ``"A + B"`` join, or a bare phase name
    that is not a collision. A bare colliding name therefore always means the
    aqueous species.
    """
    if " + " in label:
        return True
    if label.endswith(SOLID_SUFFIX) and label[: -len(SOLID_SUFFIX)] in solid_set:
        return True
    return label in solid_set and label not in collision_names


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


def subsets_for_job(params: GridJobParams) -> list[tuple[str, ...]]:
    """Element subsets to actually pack/trace for this job.

    With the per-element filter ON, every element subset is computed so the UI
    can filter the predominance map by element without recomputing. With it OFF,
    only the single full-system map is computed (the "main predominance").
    A one-element system has only one subset — per-element mode is ignored.
    """
    full = tuple(sorted(params.system_elements))
    if not full:
        return []
    if params.layer_elements and len(full) > 1:
        return subsets_to_pack(full)
    return [full]


def effective_layer_elements(
    system_elements: tuple[str, ...] | list[str],
    layer_elements: bool,
) -> bool:
    """Per-element subset maps are only meaningful for multi-element systems."""
    return bool(layer_elements) and len(set(system_elements)) > 1


def count_layer_pack_steps(params: GridJobParams) -> int:
    """Number of pack/trace layer passes enabled for this job."""
    n_subsets = len(subsets_for_job(params))
    n = 0
    if params.layer_solids:
        n += n_subsets
    if params.layer_aqueous:
        n += n_subsets
    return max(n, 1)


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
    synth = row.get("synthetic_label")
    if synth:
        return synth
    eligible = {p for p in job_phases if p in eligible_phases and not is_gas(p)}
    si = row.get("si") or {}
    finite = {p: si[p] for p in eligible if p in si and si[p] == si[p]}
    if finite:
        phase, value = max(finite.items(), key=lambda kv: kv[1])
        if value >= 0.0:
            return solid_label(phase, collision_names)
    return dominant_aq_in_subset(row, set(subset))


def dominant_aq_species_subset(row: dict, subset: set[str]) -> str:
    """Predominant aqueous species among those containing an element in ``subset``.

    Ranks by the per-element moles reported by PHREEQC's ``SYS`` so a species
    that contains a subset element (even alongside others, e.g. ``FeHCO3+`` for
    ``Fe``) is a valid candidate. Mirrors ``category_solid_subset`` for
    aqueous-only predominance maps."""
    synth = row.get("synthetic_label")
    if synth:
        return synth
    if not row.get("converged"):
        return "none"
    by_elem = row.get("aq_species_by_element")
    if by_elem:
        best_sp, best_m = "none", -1.0
        for elem in subset:
            for sp, m in by_elem.get(elem, ()):
                if sp in EXCLUDED_SPECIES:
                    continue
                if m > best_m:
                    best_m, best_sp = m, sp
        if best_sp != "none":
            return best_sp
        return dominant_aq_in_subset(row, subset)
    # Fallback when per-element rankings are absent: use flat species maps.
    mols = row.get("aq_molality_by_species") or {}
    elem_map = row.get("aq_species_element") or {}
    best_sp, best_m = "none", -1.0
    for sp, m in mols.items():
        if sp in EXCLUDED_SPECIES:
            continue
        if elem_map.get(sp) in subset and m > best_m:
            best_m, best_sp = m, sp
    if best_sp != "none":
        return best_sp
    return dominant_aq_in_subset(row, subset)


def pack_custom_category_grid(
    rows: list[dict],
    *,
    cat_fn: Callable[[dict], str],
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    pe_levels: int,
    ph_levels: int,
) -> dict[str, Any]:
    categories: set[str] = set()
    for row in rows:
        categories.add(cat_fn(row) or "none")
    names = sorted(categories)
    index = {name: i for i, name in enumerate(names)}
    grid = np.full((pe_levels, ph_levels), -1, dtype=int)
    for row in rows:
        ix = ph_lookup.get(round(float(row["ph"]), 12))
        iy = pe_lookup.get(round(float(row["pe"]), 12))
        if ix is None or iy is None:
            continue
        cat = cat_fn(row) or "none"
        grid[iy, ix] = index.get(cat, -1)
    return {"names": names, "grid": grid.tolist()}


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

    subset_list = subsets_for_job(params)
    pack_steps = count_layer_pack_steps(params)
    step = 0

    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset

    def tick() -> None:
        nonlocal step
        step += 1
        if progress_cb:
            progress_cb(step, pack_steps)

    solid_subsets: dict[str, Any] = {}
    if params.layer_solids:
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

    aqueous_subsets: dict[str, Any] = {}
    if params.layer_aqueous:
        for subset in subset_list:
            key = subset_key(subset)
            sset = set(subset)

            def aq_cat(row: dict, s: set[str] = sset) -> str:
                return dominant_aq_species_subset(row, s)

            layer = pack_custom_category_grid(
                rows,
                cat_fn=aq_cat,
                ph_lookup=ph_lookup,
                pe_lookup=pe_lookup,
                pe_levels=params.pe_levels,
                ph_levels=params.ph_levels,
            )
            layer["elements"] = list(subset)
            aqueous_subsets[key] = layer
            tick()

    layers: dict[str, Any] = {
        "solid_subsets": solid_subsets,
        "aqueous_subsets": aqueous_subsets,
    }

    default_key = subset_key(params.system_elements)
    if params.layer_solids and default_key in solid_subsets:
        default_layer = solid_subsets[default_key]
    elif params.layer_aqueous and default_key in aqueous_subsets:
        default_layer = aqueous_subsets[default_key]
    elif solid_subsets:
        default_layer = next(iter(solid_subsets.values()))
    elif aqueous_subsets:
        default_layer = next(iter(aqueous_subsets.values()))
    else:
        default_layer = {"names": [], "grid": []}

    return {
        "ph": ph.tolist(),
        "pe": pe.tolist(),
        "redox_axis": getattr(params, "redox_axis", config.REDOX_AXIS_PE),
        "system_elements": list(params.system_elements),
        "layer_solids": params.layer_solids,
        "layer_aqueous": params.layer_aqueous,
        "layer_elements": effective_layer_elements(
            params.system_elements, params.layer_elements
        ),
        "layers": layers,
        "phase_names": default_layer["names"],
        "grid": default_layer["grid"],
        "hover_species": pack_hover_species_grid(
            rows,
            ph_lookup=ph_lookup,
            pe_lookup=pe_lookup,
            pe_levels=params.pe_levels,
            ph_levels=params.ph_levels,
        ),
        "n_converged": sum(1 for r in rows if r.get("converged")),
        "n_total": len(rows),
        "temp_c": params.temp_c,
        "solution_mode": params.solution_mode,
        "diagram_kind": "predominance",
    }


MineralCategoryMode = Literal["moles", "costability"]


def mineral_subset_category_fn(
    *,
    subset: tuple[str, ...],
    eligible_phases: frozenset[str],
    job_phases: tuple[str, ...],
    collision_names: frozenset[str],
    category_mode: MineralCategoryMode = "moles",
) -> Callable[[dict], str]:
    """Row → mineral-stability category for one element subset (packing / vectors)."""
    from ..phreeqc.mineral_stability import (
        category_costability_subset,
        category_precip_subset,
    )

    if category_mode == "costability":

        def cat_fn(row: dict) -> str:
            return category_costability_subset(
                row,
                subset,
                eligible_phases=eligible_phases,
                job_phases=job_phases,
                collision_names=collision_names,
            )

        return cat_fn

    def cat_fn(row: dict) -> str:
        return category_precip_subset(
            row,
            subset,
            eligible_phases=eligible_phases,
            job_phases=job_phases,
            collision_names=collision_names,
        )

    return cat_fn


def pack_mineral_subset_grid(
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
    category_mode: MineralCategoryMode = "moles",
) -> dict[str, Any]:
    """Pack one mineral-stability subset layer (moles or post-precip costability)."""
    cat_fn = mineral_subset_category_fn(
        subset=subset,
        eligible_phases=eligible_phases,
        job_phases=job_phases,
        collision_names=collision_names,
        category_mode=category_mode,
    )
    layer = pack_custom_category_grid(
        rows,
        cat_fn=cat_fn,
        ph_lookup=ph_lookup,
        pe_lookup=pe_lookup,
        pe_levels=pe_levels,
        ph_levels=ph_levels,
    )
    solid_set = set(job_phases)
    aqueous_names = [
        n for n in layer["names"] if not label_is_solid(n, solid_set, collision_names)
    ]
    layer["elements"] = list(subset)
    layer["aqueous_names"] = aqueous_names
    return layer


def pack_mineral_grid_results(
    params: GridJobParams,
    rows: list[dict],
    *,
    category_mode: MineralCategoryMode = "moles",
    db_path: str | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Pack assemblage grid rows into mineral-stability hover/category layers.

    Parallel to ``pack_grid_results``, but solid maps use precipitated-mole
    categories and are stored under ``layers.mineral_subsets``. Sets
    ``diagram_kind="assemblage"`` so callers can keep SI predominance packs
    separate. Does not alter ``pack_grid_results``.
    """
    del db_path
    mode: MineralCategoryMode = (
        "costability" if category_mode == "costability" else "moles"
    )
    ph = np.linspace(params.ph_min, params.ph_max, params.ph_levels)
    pe = np.linspace(params.pe_min, params.pe_max, params.pe_levels)

    ph_lookup = {round(float(value), 12): i for i, value in enumerate(ph)}
    pe_lookup = {round(float(value), 12): i for i, value in enumerate(pe)}

    subset_list = subsets_for_job(params)
    pack_steps = count_layer_pack_steps(params)
    step = 0

    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset

    def tick() -> None:
        nonlocal step
        step += 1
        if progress_cb:
            progress_cb(step, pack_steps)

    mineral_subsets: dict[str, Any] = {}
    if params.layer_solids:
        for subset in subset_list:
            key = subset_key(subset)
            eligible = frozenset(subset_map.get(key, ()))
            mineral_subsets[key] = pack_mineral_subset_grid(
                rows,
                subset=subset,
                eligible_phases=eligible,
                job_phases=params.phases,
                ph_lookup=ph_lookup,
                pe_lookup=pe_lookup,
                pe_levels=params.pe_levels,
                ph_levels=params.ph_levels,
                collision_names=collisions,
                category_mode=mode,
            )
            tick()

    aqueous_subsets: dict[str, Any] = {}
    if params.layer_aqueous:
        for subset in subset_list:
            key = subset_key(subset)
            sset = set(subset)

            def aq_cat(row: dict, s: set[str] = sset) -> str:
                return dominant_aq_species_subset(row, s)

            layer = pack_custom_category_grid(
                rows,
                cat_fn=aq_cat,
                ph_lookup=ph_lookup,
                pe_lookup=pe_lookup,
                pe_levels=params.pe_levels,
                ph_levels=params.ph_levels,
            )
            layer["elements"] = list(subset)
            aqueous_subsets[key] = layer
            tick()

    layers: dict[str, Any] = {
        "mineral_subsets": mineral_subsets,
        "aqueous_subsets": aqueous_subsets,
    }

    default_key = subset_key(params.system_elements)
    if params.layer_solids and default_key in mineral_subsets:
        default_layer = mineral_subsets[default_key]
    elif params.layer_aqueous and default_key in aqueous_subsets:
        default_layer = aqueous_subsets[default_key]
    elif mineral_subsets:
        default_layer = next(iter(mineral_subsets.values()))
    elif aqueous_subsets:
        default_layer = next(iter(aqueous_subsets.values()))
    else:
        default_layer = {"names": [], "grid": []}

    return {
        "ph": ph.tolist(),
        "pe": pe.tolist(),
        "redox_axis": getattr(params, "redox_axis", config.REDOX_AXIS_PE),
        "system_elements": list(params.system_elements),
        "layer_solids": params.layer_solids,
        "layer_aqueous": params.layer_aqueous,
        "layer_elements": effective_layer_elements(
            params.system_elements, params.layer_elements
        ),
        "layers": layers,
        "phase_names": default_layer["names"],
        "grid": default_layer["grid"],
        "hover_species": pack_hover_species_grid(
            rows,
            ph_lookup=ph_lookup,
            pe_lookup=pe_lookup,
            pe_levels=params.pe_levels,
            ph_levels=params.ph_levels,
        ),
        "hover_precip": pack_hover_precip_grid(
            rows,
            ph_lookup=ph_lookup,
            pe_lookup=pe_lookup,
            pe_levels=params.pe_levels,
            ph_levels=params.ph_levels,
            collision_names=collisions,
        ),
        "n_converged": sum(1 for r in rows if r.get("converged")),
        "n_total": len(rows),
        "temp_c": params.temp_c,
        "solution_mode": params.solution_mode,
        "diagram_kind": "assemblage",
        "mineral_category_mode": mode,
    }
