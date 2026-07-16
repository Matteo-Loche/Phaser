"""Mineral-stability boundary tracing (moles + post-precip costability).

Reuses the generic geometry / ProcessPool / brentq machinery in
``boundary_trace.py`` via:

  - ``trace_mode="mineral_moles"`` (alias ``"mineral"``)
  - ``trace_mode="mineral_costability"`` (alias ``"mineral_si"`` —
    historical name; costability is post-precip moles, not free SI)

Both modes require assemblage EQUILIBRIUM_PHASES grids. Legacy SI
predominance tracing (``trace_mode="predominance"``) stays untouched.

Layer ids:
  - ``mineral:<subset>`` — moles or costability categories
  - ``aqueous:<subset>`` — aqueous species predominance
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

import numpy as np

from .. import config
from ..diagram.packer import (
    dominant_aq_species_subset,
    label_is_solid,
    subset_key,
    subsets_for_job,
)
from ..services.job_control import check_abort
from .boundary_trace import LayerSpec, TraceStats, _chunk_cells, run_boundary_trace
from .dummy_medium import EXCLUDED_SPECIES
from .engine import GridJobParams, GridPointResult
from .mineral_stability import (
    MineralCategoryMode,
    _normalize_category_mode,
    category_costability_subset,
    category_precip_subset,
    mineral_stability_signature_fn,
    resolve_mineral_costability_pair_scalar,
    resolve_mineral_moles_pair_scalar,
)
from .sweep import _point_key, build_grid, run_point_sweep

_TRACE_MODE_BY_CATEGORY = {
    "moles": "mineral_moles",
    "costability": "mineral_costability",
}


def mineral_resolve_pair_for_mode(
    category_mode: MineralCategoryMode,
) -> Callable[
    [str, str, frozenset[str], frozenset[str]],
    tuple[Callable[[dict], float | None] | None, str],
]:
    """Return a ``resolve_pair`` callable bound to moles or costability."""
    mode = _normalize_category_mode(category_mode)
    resolve = (
        resolve_mineral_costability_pair_scalar
        if mode == "costability"
        else resolve_mineral_moles_pair_scalar
    )

    def _bound(
        cat_a: str,
        cat_b: str,
        solid_phases: frozenset[str] = frozenset(),
        collisions: frozenset[str] = frozenset(),
    ) -> tuple[Callable[[dict], float | None] | None, str]:
        return resolve(
            cat_a,
            cat_b,
            solid_phases=solid_phases,
            collisions=collisions,
        )

    return _bound


def mineral_layer_specs(
    params: GridJobParams,
    db_path: str | None = None,
    *,
    category_mode: MineralCategoryMode = "moles",
) -> list[LayerSpec]:
    """Layer factories for mineral stability + aqueous species."""
    del db_path
    mode = _normalize_category_mode(category_mode)
    job_phases = params.phases
    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset
    specs: list[LayerSpec] = []

    for subset in subsets_for_job(params):
        key = subset_key(subset)
        eligible = frozenset(subset_map.get(key, ()))

        if params.layer_solids:
            if mode == "costability":

                def mineral_cat(
                    row: dict,
                    subset: tuple[str, ...] = subset,
                    elig: frozenset[str] = eligible,
                ) -> str:
                    return category_costability_subset(
                        row,
                        subset,
                        eligible_phases=elig,
                        job_phases=job_phases,
                        collision_names=collisions,
                    )

            else:

                def mineral_cat(
                    row: dict,
                    subset: tuple[str, ...] = subset,
                    elig: frozenset[str] = eligible,
                ) -> str:
                    return category_precip_subset(
                        row,
                        subset,
                        eligible_phases=elig,
                        job_phases=job_phases,
                        collision_names=collisions,
                    )

            specs.append(LayerSpec(layer_id=f"mineral:{key}", cat_fn=mineral_cat))

        if params.layer_aqueous:

            def aq_cat(row: dict, subset: tuple[str, ...] = subset) -> str:
                return dominant_aq_species_subset(row, set(subset))

            specs.append(LayerSpec(layer_id=f"aqueous:{key}", cat_fn=aq_cat))

    return specs


def collect_mineral_trace_species(
    params: GridJobParams,
    base_ij: dict[tuple[int, int], Any],
    cells: list[tuple[int, int]],
    specs: list[LayerSpec],
) -> tuple[str, ...]:
    """Species for ``-mol`` during mineral traces; skip co-precip join labels."""
    from .boundary_trace import _corner_cats

    solid_set = frozenset(params.phases)
    collisions = frozenset(params.solid_aqueous_collisions)
    names: set[str] = set()
    for key in base_ij:
        r = base_ij[key]
        row = r if isinstance(r, dict) else asdict(r)
        names.update(
            sp
            for sp in (row.get("aq_molality_by_species") or {})
            if sp not in EXCLUDED_SPECIES
        )
        for sp in (row.get("dominant_aq_by_element") or {}).values():
            if sp and sp not in ("none", "aqueous") and sp not in EXCLUDED_SPECIES:
                names.add(sp)
    for spec in specs:
        for i, j in cells:
            for cat in _corner_cats(i, j, spec.cat_fn, base_ij):
                if cat in ("none", "aqueous"):
                    continue
                if " + " in cat:
                    continue
                if label_is_solid(cat, solid_set, collisions):
                    continue
                names.add(cat)
    return tuple(sorted(names))


def run_mineral_boundary_trace(
    params: GridJobParams,
    *,
    db_path: str,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], GridPointResult],
    cells: list[tuple[int, int]],
    tolerance: float | None = None,
    stability_tolerance: float | None = None,
    refine_factor: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    max_workers: int | None = None,
    job_id: str | None = None,
    category_mode: MineralCategoryMode = "moles",
) -> tuple[dict[str, Any], TraceStats]:
    """Trace mineral-stability boundaries (same bundle shape as predominance)."""
    mode = _normalize_category_mode(category_mode)
    return run_boundary_trace(
        params,
        db_path=db_path,
        base_ph=base_ph,
        base_pe=base_pe,
        base_ij=base_ij,
        cells=cells,
        tolerance=tolerance,
        stability_tolerance=stability_tolerance,
        refine_factor=refine_factor,
        progress_cb=progress_cb,
        max_workers=max_workers,
        job_id=job_id,
        trace_mode=_TRACE_MODE_BY_CATEGORY[mode],
    )


def run_adaptive_mineral_stability_sweep(
    params: GridJobParams,
    *,
    max_workers: int | None = None,
    progress_cb=None,
    refine_factor: int | None = None,
    job_id: str | None = None,
    category_mode: MineralCategoryMode = "moles",
) -> tuple[GridJobParams, dict[str, Any], list[GridPointResult], dict[str, Any] | None]:
    """Base grid + mineral-stability boundary tracing.

    Both ``moles`` and ``costability`` require assemblage ``solution_mode``.
    """
    from .adaptive import boundary_base_cells
    from .gas_limits import trace_gas_limit_segments

    mode = _normalize_category_mode(category_mode)
    trace_mode = _TRACE_MODE_BY_CATEGORY[mode]
    check_abort(job_id)
    base_ph, base_pe = build_grid(params)
    n_ph = params.ph_levels
    n_pe = params.pe_levels
    base_total = n_ph * n_pe
    if base_total > config.MAX_GRID_POINTS:
        raise ValueError(
            f"Grid has {base_total} points; limit is {config.MAX_GRID_POINTS}. "
            "Reduce ph_levels or pe_levels."
        )

    desired = refine_factor or config.ADAPTIVE_REFINE_FACTOR

    def report(done: int, total: int, phase: str) -> None:
        if progress_cb:
            progress_cb(done, total, phase)

    base_points = [(float(p), float(e)) for e in base_pe for p in base_ph]
    from .sweep import partition_points_for_sweep

    _, _, mask_stats = partition_points_for_sweep(params, base_points)
    base_rows = run_point_sweep(
        params,
        base_points,
        max_workers=max_workers,
        progress_cb=(lambda d, _t: report(d, base_total, "grid")) if progress_cb else None,
        job_id=job_id,
    )
    check_abort(job_id)
    base_by_key = {_point_key(r.ph, r.pe): r for r in base_rows}

    signature = mineral_stability_signature_fn(params, category_mode=mode)
    categories = np.empty((n_pe, n_ph), dtype=object)
    base_result_ij: dict[tuple[int, int], GridPointResult] = {}
    for j in range(n_pe):
        for i in range(n_ph):
            row = base_by_key[_point_key(float(base_ph[i]), float(base_pe[j]))]
            categories[j, i] = signature(asdict(row))
            base_result_ij[(i, j)] = row

    cells = boundary_base_cells(categories)

    if not cells:
        report(base_total, base_total, "grid")
        gas_segments = trace_gas_limit_segments(
            params,
            base_ph=base_ph,
            base_pe=base_pe,
            base_ij=base_result_ij,
        )
        stats = {
            "n_evaluated": base_total,
            "n_total": base_total,
            "n_filled": 0,
            "base_levels_ph": n_ph,
            "base_levels_pe": n_pe,
            "refine_factor": 1,
            "boundary_cells": 0,
            "n_boundary_evaluated": 0,
            "n_gas_segments": len(gas_segments),
            "refinement_method": "trace",
            "display_mode": "traced",
            "trace_mode": trace_mode,
            "mineral_category_mode": mode,
            "n_skipped_water": mask_stats.get("n_skipped_water", 0),
        }
        trace_bundle = {
            "method": "traced",
            "trace_mode": trace_mode,
            "mineral_category_mode": mode,
            "refine_factor": 1,
            "layers": {},
            "stability_limits": {"kind": "stability_limit", "segments": []},
            "gas_limits": {"kind": "gas_limit", "segments": gas_segments},
        }
        return params, stats, base_rows, trace_bundle

    workers = max_workers if max_workers is not None else config.MAX_WORKERS
    n_progress = max(1, len(_chunk_cells(cells, workers=workers)))
    report(0, n_progress, "boundaries")
    trace_bundle, trace_stats = run_mineral_boundary_trace(
        params,
        db_path=params.db_path,
        base_ph=base_ph,
        base_pe=base_pe,
        base_ij=base_result_ij,
        cells=cells,
        refine_factor=desired,
        max_workers=max_workers,
        progress_cb=(
            (lambda d, t: report(d, t, "boundaries")) if progress_cb else None
        ),
        job_id=job_id,
        category_mode=mode,
    )
    report(n_progress, n_progress, "boundaries")
    if isinstance(trace_bundle, dict):
        trace_bundle = {**trace_bundle, "mineral_category_mode": mode}

    n_trace = trace_stats.n_trace_evals + trace_stats.n_fallback_evals
    stats = {
        "n_evaluated": base_total + n_trace,
        "n_total": base_total,
        "n_filled": 0,
        "base_levels_ph": n_ph,
        "base_levels_pe": n_pe,
        "refine_factor": desired,
        "boundary_cells": len(cells),
        "n_boundary_evaluated": n_trace,
        "n_trace_evals": trace_stats.n_trace_evals,
        "n_fallback_evals": trace_stats.n_fallback_evals,
        "n_trace_segments": trace_stats.n_segments,
        "n_stability_segments": trace_stats.n_stability_segments,
        "n_gas_segments": trace_stats.n_gas_segments,
        "n_brentq_mol": trace_stats.n_brentq_mol,
        "n_brentq_si": trace_stats.n_brentq_si,
        "n_brentq_aq": trace_stats.n_brentq_aq,
        "refinement_method": "trace",
        "display_mode": "traced",
        "trace_mode": trace_mode,
        "mineral_category_mode": mode,
        "n_skipped_water": mask_stats.get("n_skipped_water", 0),
    }
    return params, stats, base_rows, trace_bundle
