"""Adaptive boundary tracing for phase-diagram grid sweeps.

Pipeline:

1. Evaluate the full user-selected base grid (hover data + corner categories).
2. Use catalog-derived solid/aqueous collision labels; colliding solids are suffixed
   ``<name>(s)`` on ``GridJobParams`` before tracing.
3. Flag base cells whose corners differ across any plottable layer signature.
4. Trace phase boundaries on those cells via root-finding (``boundary_trace.py``),
   emitting exact line segments and convex fill regions for 3-category cells.
5. Pack traced geometry into vector display layers (``diagram/vectors.py``).
"""
from __future__ import annotations

from dataclasses import asdict
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


def layer_signature_fn(params: GridJobParams) -> Callable[[dict], tuple]:
    """Build a row -> signature function spanning every plottable layer."""
    from ..phreeqc.catalog import is_gas
    from ..diagram.packer import dominant_aq_in_subset, solid_label, subset_key, subsets_to_pack

    elements = params.system_elements
    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset

    eligible_by_subset: list[tuple[set[str], tuple[str, ...]]] = []
    for subset in subsets_to_pack(params.system_elements):
        sset = set(subset)
        elig = tuple(
            p for p in params.phases
            if not is_gas(p) and p in subset_map.get(subset_key(subset), ())
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
                    parts.append(solid_label(phase, collisions))
                    continue
            parts.append(dominant_aq_in_subset(row, sset))
        return tuple(parts)

    return signature


def boundary_base_cells(categories: np.ndarray) -> list[tuple[int, int]]:
    """Base cells (lower-left index i,j) whose 4 corners differ in category."""
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


def run_adaptive_boundary_sweep(
    params: GridJobParams,
    *,
    max_workers: int | None = None,
    progress_cb=None,
    refine_factor: int | None = None,
) -> tuple[GridJobParams, dict[str, Any], list[GridPointResult], dict[str, Any] | None]:
    """Full base grid sweep + boundary tracing.

    Returns ``(base_params, stats, base_rows, trace_bundle)``.
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

    from .gas_limits import trace_gas_limit_segments

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
        }
        trace_bundle = {
            "method": "traced",
            "refine_factor": 1,
            "layers": {},
            "stability_limits": {"kind": "stability_limit", "segments": []},
            "gas_limits": {"kind": "gas_limit", "segments": gas_segments},
        }
        return params, stats, base_rows, trace_bundle

    from .boundary_trace import _chunk_cells, run_boundary_trace

    workers = max_workers or min(config.MAX_WORKERS, 4)
    n_progress = max(1, len(_chunk_cells(cells, workers=workers)))
    report(0, n_progress, "boundaries")
    trace_bundle, trace_stats = run_boundary_trace(
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
    )
    report(n_progress, n_progress, "boundaries")

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
        "refinement_method": "trace",
        "display_mode": "traced",
    }
    return params, stats, base_rows, trace_bundle
