"""Adaptive boundary refinement for phase-diagram grid sweeps.

Strategy ("hunt and track boundaries"):

1. Evaluate the full user-selected grid (the *base* grid). This guarantees no
   phase region is missed, and the base grid is kept as hoverable data.
2. Find base cells whose four corners are not all the same dominant category
   (these straddle a phase boundary).
3. Subdivide only those boundary cells by ``refine_factor`` and evaluate the
   new sub-grid points with PHREEQC.
4. Upscale the rest of the diagram from the base grid (block fill, no compute).

The result is packed at the finer resolution, so boundaries look much sharper
while total PHREEQC runs stay far below a uniform fine grid.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Callable

import numpy as np

from .. import config
from .engine import GridJobParams, GridPointResult
from .sweep import _point_key, build_grid, run_point_sweep


def fine_axis_levels(base_levels: int, factor: int) -> int:
    """Fine-axis length so every base node lands exactly on a fine node."""
    if base_levels <= 1 or factor <= 1:
        return max(1, base_levels)
    return (base_levels - 1) * factor + 1


def category_label(row: GridPointResult) -> str:
    if not row.converged:
        return "none"
    return row.dominant_solid or "aqueous"


def layer_signature_fn(params: GridJobParams) -> Callable[[dict], tuple]:
    """Build a row -> signature function spanning every plottable layer.

    The diagram renders one layer per solid-element subset (Fe, Cu, Fe-Cu, ...)
    and one aqueous map per element. Refining only the full-system solid
    predominance leaves the other layers blocky. The signature concatenates the
    category each layer would show, so a base cell is treated as a boundary cell
    when *any* layer changes across its corners.
    """
    from ..db.parser import is_gas
    from ..diagram.packer import dominant_aq_in_subset, subsets_to_pack
    from ..diagram.phases import phase_element_map

    phase_elements = phase_element_map(params.db_path)
    elements = params.system_elements

    # Precompute the eligible solid phases for each subset once (independent of
    # the grid point), so the per-point signature stays cheap on large grids.
    eligible_by_subset: list[tuple[set[str], tuple[str, ...]]] = []
    for subset in subsets_to_pack(params.system_elements):
        sset = set(subset)
        elig = tuple(
            p for p in params.phases
            if not is_gas(p) and phase_elements.get(p, frozenset()).issubset(sset)
        )
        eligible_by_subset.append((sset, elig))

    def signature(row: dict) -> tuple:
        if not row.get("converged"):
            return ("__none__",)
        parts: list[str] = []
        aq = row.get("dominant_aq_by_element") or {}
        for elem in elements:
            parts.append(aq.get(elem, "none"))
        si = row.get("si") or {}
        for sset, elig in eligible_by_subset:
            finite = [(p, si[p]) for p in elig if p in si and si[p] == si[p]]
            if finite:
                phase, value = max(finite, key=lambda kv: kv[1])
                if value >= 0.0:
                    parts.append(phase)
                    continue
            parts.append(dominant_aq_in_subset(row, sset))
        return tuple(parts)

    return signature


def boundary_base_cells(categories: np.ndarray) -> list[tuple[int, int]]:
    """Base cells (lower-left index i,j) whose 4 corners differ in category.

    ``categories`` is indexed ``[j, i]`` (pe row, pH column).
    """
    n_pe, n_ph = categories.shape
    cells: list[tuple[int, int]] = []
    for j in range(n_pe - 1):
        for i in range(n_ph - 1):
            c = categories[j, i]
            if (
                categories[j, i + 1] != c
                or categories[j + 1, i] != c
                or categories[j + 1, i + 1] != c
            ):
                cells.append((i, j))
    return cells


def fine_nodes_for_cells(
    cells: list[tuple[int, int]],
    factor: int,
    n_ph_fine: int,
    n_pe_fine: int,
) -> set[tuple[int, int]]:
    """Fine-grid indices (fi, fj) covered by the given base cells."""
    nodes: set[tuple[int, int]] = set()
    for i, j in cells:
        fi0 = i * factor
        fj0 = j * factor
        for fj in range(fj0, min(fj0 + factor, n_pe_fine - 1) + 1):
            for fi in range(fi0, min(fi0 + factor, n_ph_fine - 1) + 1):
                nodes.add((fi, fj))
    return nodes


def choose_refine_factor(
    n_ph: int,
    n_pe: int,
    boundary_cell_count: int,
    desired_factor: int,
    budget: int,
) -> int:
    """Largest factor whose extra sub-cell evaluations fit the compute budget."""
    for factor in range(max(2, desired_factor), 1, -1):
        approx_new = boundary_cell_count * (factor + 1) * (factor + 1)
        if approx_new <= budget:
            return factor
    return 1


def estimate_adaptive_points(
    ph_levels: int,
    pe_levels: int,
    *,
    refine_factor: int | None = None,
    boundary_fraction: float = 0.12,
) -> int:
    """Rough UI estimate: full base grid + sub-cells for ~boundary_fraction cells."""
    n_ph = max(1, ph_levels)
    n_pe = max(1, pe_levels)
    factor = refine_factor or config.ADAPTIVE_REFINE_FACTOR
    base = n_ph * n_pe
    base_cells = max(0, (n_ph - 1) * (n_pe - 1))
    boundary_cells = int(base_cells * boundary_fraction)
    new_per_cell = (factor + 1) * (factor + 1) - 4
    return min(config.MAX_ADAPTIVE_POINTS, base + boundary_cells * max(0, new_per_cell))


def _clone_result_at(row: GridPointResult, ph: float, pe: float) -> GridPointResult:
    return GridPointResult(
        ph=ph,
        pe=pe,
        converged=row.converged,
        dominant_phase=row.dominant_phase,
        dominant_solid=row.dominant_solid,
        dominant_aq_by_element=dict(row.dominant_aq_by_element),
        aq_molality_by_element=dict(row.aq_molality_by_element),
        si=dict(row.si),
    )


def run_adaptive_boundary_sweep(
    params: GridJobParams,
    *,
    max_workers: int | None = None,
    progress_cb=None,
    refine_factor: int | None = None,
) -> tuple[list[GridPointResult], GridJobParams, dict[str, Any]]:
    """Full base grid + boundary subdivision, returned at the finer resolution.

    Returns ``(fine_rows, pack_params, stats)`` where ``pack_params`` carries the
    finer ``ph_levels``/``pe_levels`` for :func:`pack_grid_results`.
    """
    base_ph, base_pe = build_grid(params)
    n_ph = params.ph_levels
    n_pe = params.pe_levels
    base_total = n_ph * n_pe
    if base_total > config.MAX_GRID_POINTS:
        raise ValueError(
            f"Grid has {base_total} points; limit is {config.MAX_GRID_POINTS}. "
            "Reduce ph_levels or pe_levels."
        )

    # Progress is reported per phase ("grid" then "boundaries") so the bar
    # reflects the actual adaptive work. It deliberately resets between phases
    # rather than faking a single monotonic estimate.
    desired = refine_factor or config.ADAPTIVE_REFINE_FACTOR

    def report(done: int, total: int, phase: str) -> None:
        if progress_cb:
            progress_cb(done, total, phase)

    base_points = [(float(p), float(e)) for e in base_pe for p in base_ph]
    base_rows = run_point_sweep(
        params,
        base_points,
        max_workers=max_workers,
        progress_cb=(lambda d, _t: report(d, base_total, "grid")) if progress_cb else None,
    )
    base_by_key = {_point_key(r.ph, r.pe): r for r in base_rows}

    signature = layer_signature_fn(params)
    categories = np.empty((n_pe, n_ph), dtype=object)
    base_result_ij: dict[tuple[int, int], GridPointResult] = {}
    for j in range(n_pe):
        for i in range(n_ph):
            row = base_by_key[_point_key(float(base_ph[i]), float(base_pe[j]))]
            categories[j, i] = signature(asdict(row))
            base_result_ij[(i, j)] = row

    cells = boundary_base_cells(categories)
    budget = max(0, config.MAX_ADAPTIVE_POINTS - base_total)
    factor = choose_refine_factor(n_ph, n_pe, len(cells), desired, budget)

    # No boundaries or no budget: nothing to refine, return base grid as-is.
    if factor <= 1 or not cells:
        report(base_total, base_total, "grid")
        stats = {
            "n_evaluated": base_total,
            "n_total": base_total,
            "n_filled": 0,
            "base_levels_ph": n_ph,
            "base_levels_pe": n_pe,
            "fine_levels_ph": n_ph,
            "fine_levels_pe": n_pe,
            "refine_factor": 1,
            "boundary_cells": len(cells),
            "n_boundary_evaluated": 0,
        }
        return base_rows, params, stats

    n_ph_fine = fine_axis_levels(n_ph, factor)
    n_pe_fine = fine_axis_levels(n_pe, factor)
    fine_ph = np.linspace(params.ph_min, params.ph_max, n_ph_fine)
    fine_pe = np.linspace(params.pe_min, params.pe_max, n_pe_fine)

    # Seed fine grid with base results at aligned nodes.
    evaluated: dict[tuple[int, int], GridPointResult] = {}
    for (i, j), row in base_result_ij.items():
        fi, fj = i * factor, j * factor
        evaluated[(fi, fj)] = _clone_result_at(row, float(fine_ph[fi]), float(fine_pe[fj]))

    refine_nodes = fine_nodes_for_cells(cells, factor, n_ph_fine, n_pe_fine)
    to_eval = [(fi, fj) for (fi, fj) in sorted(refine_nodes) if (fi, fj) not in evaluated]
    refine_points = [(float(fine_ph[fi]), float(fine_pe[fj])) for fi, fj in to_eval]
    eval_total = base_total + len(refine_points)
    n_refine = len(refine_points)

    if refine_points:
        # Second phase: report progress within the boundary-refinement pass.
        report(0, n_refine, "boundaries")
        refine_rows = run_point_sweep(
            params,
            refine_points,
            max_workers=max_workers,
            progress_cb=(lambda d, _t: report(d, n_refine, "boundaries")) if progress_cb else None,
        )
        refine_by_key = {_point_key(r.ph, r.pe): r for r in refine_rows}
        for (fi, fj), (ph, pe) in zip(to_eval, refine_points, strict=True):
            evaluated[(fi, fj)] = refine_by_key[_point_key(ph, pe)]

    # Assemble the full fine grid; fill non-evaluated nodes from nearest base node.
    fine_rows: list[GridPointResult] = []
    filled = 0
    for fj in range(n_pe_fine):
        for fi in range(n_ph_fine):
            row = evaluated.get((fi, fj))
            if row is None:
                bi = min(n_ph - 1, max(0, round(fi / factor)))
                bj = min(n_pe - 1, max(0, round(fj / factor)))
                row = _clone_result_at(
                    base_result_ij[(bi, bj)],
                    float(fine_ph[fi]),
                    float(fine_pe[fj]),
                )
                filled += 1
            fine_rows.append(row)

    report(n_refine, n_refine, "boundaries")

    pack_params = replace(params, ph_levels=n_ph_fine, pe_levels=n_pe_fine)
    stats = {
        "n_evaluated": base_total + len(refine_points),
        "n_total": n_ph_fine * n_pe_fine,
        "n_filled": filled,
        "base_levels_ph": n_ph,
        "base_levels_pe": n_pe,
        "fine_levels_ph": n_ph_fine,
        "fine_levels_pe": n_pe_fine,
        "refine_factor": factor,
        "boundary_cells": len(cells),
        "n_boundary_evaluated": len(refine_points),
    }
    return fine_rows, pack_params, stats
