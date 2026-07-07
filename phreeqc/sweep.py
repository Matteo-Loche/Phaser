"""Multiprocessing grid sweep for phase diagrams."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from itertools import product

import numpy as np

from .. import config
from .engine import (
    GridJobParams,
    GridPointResult,
    evaluate_point,
    grid_job_params_from_dict,
    init_phreeqc,
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
    return round(float(ph), 12), round(float(pe), 12)


def run_point_sweep(
    params: GridJobParams,
    points: list[tuple[float, float]],
    *,
    max_workers: int | None = None,
    progress_cb=None,
    progress_offset: int = 0,
    progress_total: int | None = None,
    map_chunksize: int | None = None,
) -> list[GridPointResult]:
    """Evaluate an explicit list of (pH, pe) points."""
    unique: dict[tuple[float, float], tuple[float, float]] = {}
    for ph, pe in points:
        unique[_point_key(ph, pe)] = (float(ph), float(pe))
    tasks_list = list(unique.values())
    total = progress_total if progress_total is not None else len(tasks_list)
    if not tasks_list:
        return []

    if len(params.phases) > config.MAX_PHASES_PER_JOB:
        raise ValueError(
            f"{len(params.phases)} phases selected; limit is {config.MAX_PHASES_PER_JOB}."
        )

    workers = max_workers if max_workers is not None else config.MAX_WORKERS
    chunksize = map_chunksize if map_chunksize is not None else config.SWEEP_MAP_CHUNKSIZE
    chunksize = max(1, int(chunksize))
    params_dict = asdict(params)

    results: list[GridPointResult] = []
    done = progress_offset
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(params.dll_path, params.db_path, params_dict),
    ) as pool:
        for row in pool.map(_worker_eval, tasks_list, chunksize=chunksize):
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
) -> list[GridPointResult]:
    ph_axis, pe_axis = build_grid(params)
    total = len(ph_axis) * len(pe_axis)
    if total > config.MAX_GRID_POINTS:
        raise ValueError(
            f"Grid has {total} points; limit is {config.MAX_GRID_POINTS}. "
            "Reduce ph_levels or pe_levels."
        )
    points = [(float(p), float(e)) for p, e in product(ph_axis, pe_axis)]
    return run_point_sweep(
        params,
        points,
        max_workers=max_workers,
        progress_cb=progress_cb,
        map_chunksize=map_chunksize,
    )
