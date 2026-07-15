"""Multiprocessing grid sweep for phase diagrams."""
from __future__ import annotations

from dataclasses import asdict
from itertools import product
from typing import Any

import numpy as np

from .. import config
from ..services.job_control import check_abort, managed_process_pool
from .engine import (
    GridJobParams,
    GridPointResult,
    evaluate_point,
    grid_job_params_from_dict,
    init_phreeqc,
    point_key,
)

_WORKER_PQ = None
_WORKER_PARAMS: GridJobParams | None = None


def _worker_init(dll_path: str, db_path: str, params_dict: dict) -> None:
    global _WORKER_PQ, _WORKER_PARAMS
    _WORKER_PQ = init_phreeqc(dll_path, db_path)
    _WORKER_PARAMS = grid_job_params_from_dict(params_dict)


def _worker_eval(args: tuple[float, float]) -> dict:
    ph, pe = args
    result = evaluate_point(_WORKER_PQ, ph=ph, pe=pe, params=_WORKER_PARAMS)
    return asdict(result)


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
    max_workers: int | None = None,
    progress_cb=None,
    progress_offset: int = 0,
    progress_total: int | None = None,
    apply_water_mask: bool = True,
    job_id: str | None = None,
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

    workers = max_workers if max_workers is not None else config.MAX_WORKERS
    chunksize = config.SWEEP_MAP_CHUNKSIZE
    chunksize = max(1, int(chunksize))
    params_dict = asdict(params)

    done = progress_offset
    with managed_process_pool(
        job_id,
        max_workers=workers,
        initializer=_worker_init,
        initargs=(params.dll_path, params.db_path, params_dict),
    ) as pool:
        for row in pool.map(_worker_eval, tasks_list, chunksize=chunksize):
            check_abort(job_id)
            results.append(GridPointResult(**row))
            done += 1
            if progress_cb:
                progress_cb(done, total)

    return results


def run_grid_sweep(
    params: GridJobParams,
    *,
    max_workers: int | None = None,
    progress_cb=None,
    map_chunksize: int | None = None,
    job_id: str | None = None,
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
    del map_chunksize  # reserved; run_point_sweep uses config.SWEEP_MAP_CHUNKSIZE
    rows = run_point_sweep(
        params,
        points,
        max_workers=max_workers,
        progress_cb=progress_cb,
        job_id=job_id,
    )
    return rows, mask_stats
