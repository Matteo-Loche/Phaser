"""Multiprocessing grid sweep for phase diagrams."""
from __future__ import annotations

from dataclasses import asdict
from itertools import product
from typing import Any

import numpy as np

from .. import config
from ..services.job_control import check_abort, pool_map_abortable
from .engine import (
    GridJobParams,
    GridPointResult,
    evaluate_point,
    point_key,
    worker_params,
    worker_phreeqc,
)
from .session import require_compute_pool

def _worker_eval(args: tuple[float, float]) -> dict:
    ph, pe = args
    result = evaluate_point(worker_phreeqc(), ph=ph, pe=pe, params=worker_params())
    return asdict(result)


def _worker_eval_chunk(chunk: list[tuple[float, float]]) -> list[dict]:
    """Evaluate a batch of points in one IPC round-trip (abortable map unit)."""
    return [_worker_eval(args) for args in chunk]


def _point_batches(
    points: list[tuple[float, float]],
    batch_size: int,
) -> list[list[tuple[float, float]]]:
    """Cut grid points into fixed batches.

    The batch size is ``SWEEP_MAP_CHUNKSIZE`` (default 50). The worker count
    does not change it. A short remainder is its own last batch.
    """
    step = int(batch_size)
    if step < 1:
        raise ValueError(
            f"PHASER_SWEEP_MAP_CHUNKSIZE must be at least 1, got {step}."
        )
    return [points[start : start + step] for start in range(0, len(points), step)]


def build_grid(params: GridJobParams) -> tuple[np.ndarray, np.ndarray]:
    ph = np.linspace(params.ph_min, params.ph_max, params.ph_levels)
    pe = np.linspace(params.pe_min, params.pe_max, params.pe_levels)
    return ph, pe


def _point_key(ph: float, pe: float) -> tuple[float, float]:
    return point_key(ph, pe)


def _grid_cell_spacings(params: GridJobParams) -> tuple[float, float]:
    cell_ph = (params.ph_max - params.ph_min) / max(params.ph_levels - 1, 1)
    cell_pe = (params.pe_max - params.pe_min) / max(params.pe_levels - 1, 1)
    return cell_ph, cell_pe


def partition_points_for_sweep(
    params: GridJobParams,
    points: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], list[GridPointResult], dict[str, int]]:
    """Split points into PHREEQC targets and pre-built synthetic outside-water rows."""
    from .gas_limits import make_synthetic_water_result, split_water_band_points

    cell_ph, cell_pe = _grid_cell_spacings(params)
    inside, outside = split_water_band_points(
        points, params, cell_ph=cell_ph, cell_pe=cell_pe
    )
    synthetic = [
        make_synthetic_water_result(ph, pe, label) for (ph, pe), label in outside
    ]
    stats = {
        "n_skipped_water": len(outside),
        "n_evaluated": len(inside),
        "n_total": len(points),
    }
    return inside, synthetic, stats


def run_point_sweep(
    params: GridJobParams,
    points: list[tuple[float, float]],
    *,
    progress_cb=None,
    progress_offset: int = 0,
    progress_total: int | None = None,
    apply_water_mask: bool = True,
    job_id: str | None = None,
    compute_pool=None,
) -> list[GridPointResult]:
    """Evaluate an explicit list of (pH, pe) points."""
    from .gas_limits import water_band_active

    check_abort(job_id)

    synthetic: list[GridPointResult] = []
    eval_points = points
    if apply_water_mask and water_band_active(params):
        eval_points, synthetic, _ = partition_points_for_sweep(params, points)

    unique: dict[tuple[float, float], tuple[float, float]] = {}
    for ph, pe in eval_points:
        unique[_point_key(ph, pe)] = (float(ph), float(pe))
    tasks_list = list(unique.values())
    total = progress_total if progress_total is not None else len(points)
    if not tasks_list and not synthetic:
        return []

    if len(params.phases) > config.MAX_PHASES_PER_JOB:
        raise ValueError(
            f"{len(params.phases)} phases selected; limit is {config.MAX_PHASES_PER_JOB}."
        )

    results: list[GridPointResult] = list(synthetic)
    if not tasks_list:
        return results

    compute_pool = require_compute_pool(compute_pool)
    workers = compute_pool.workers
    done = progress_offset

    def _mapped(pool) -> None:
        from ..services.job_control import hold_phase_for_test

        hold_phase_for_test(job_id, "grid")
        chunks = _point_batches(tasks_list, config.SWEEP_MAP_CHUNKSIZE)
        max_in_flight = max(workers * 2, 4)

        def _on_chunk(rows, _n):
            nonlocal done
            for row in rows:
                results.append(GridPointResult(**row))
                done += 1
                if progress_cb:
                    progress_cb(done, total)

        pool_map_abortable(
            pool,
            _worker_eval_chunk,
            chunks,
            job_id=job_id,
            max_in_flight=max_in_flight,
            on_result=_on_chunk,
            retain=False,
        )

    _mapped(compute_pool.pool)
    return results


def run_grid_sweep(
    params: GridJobParams,
    *,
    progress_cb=None,
    job_id: str | None = None,
    compute_pool=None,
) -> tuple[list[GridPointResult], dict[str, Any]]:
    ph_axis, pe_axis = build_grid(params)
    total = len(ph_axis) * len(pe_axis)
    if total > config.MAX_GRID_POINTS:
        raise ValueError(
            f"Grid has {total} points; limit is {config.MAX_GRID_POINTS}. "
            "Reduce ph_levels or pe_levels."
        )
    points = [(float(p), float(e)) for p, e in product(ph_axis, pe_axis)]
    _, _, mask_stats = partition_points_for_sweep(params, points)
    rows = run_point_sweep(
        params,
        points,
        progress_cb=progress_cb,
        job_id=job_id,
        compute_pool=compute_pool,
    )
    return rows, mask_stats
