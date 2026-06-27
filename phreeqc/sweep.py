"""Multiprocessing grid sweep for phase diagrams."""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from itertools import product

import numpy as np

from .. import config
from .engine import GridJobParams, GridPointResult, evaluate_point, init_phreeqc


def _worker_init(dll_path: str, db_path: str):
    global _WORKER_PQ
    _WORKER_PQ = init_phreeqc(dll_path, db_path)


def _worker_eval(args: tuple[float, float, dict]) -> dict:
    ph, pe, params_dict = args
    params = GridJobParams(**params_dict)
    result = evaluate_point(_WORKER_PQ, ph=ph, pe=pe, params=params)
    return asdict(result)


def build_grid(params: GridJobParams) -> tuple[np.ndarray, np.ndarray]:
    ph = np.linspace(params.ph_min, params.ph_max, params.ph_levels)
    pe = np.linspace(params.pe_min, params.pe_max, params.pe_levels)
    return ph, pe


def run_grid_sweep(
    params: GridJobParams,
    *,
    max_workers: int | None = None,
    progress_cb=None,
) -> list[GridPointResult]:
    ph_axis, pe_axis = build_grid(params)
    total = len(ph_axis) * len(pe_axis)
    if total > config.MAX_GRID_POINTS:
        raise ValueError(
            f"Grid has {total} points; limit is {config.MAX_GRID_POINTS}. "
            "Reduce ph_levels or pe_levels."
        )
    if len(params.phases) > config.MAX_PHASES_PER_JOB:
        raise ValueError(
            f"{len(params.phases)} phases selected; limit is {config.MAX_PHASES_PER_JOB}."
        )

    workers = max_workers or min(config.MAX_WORKERS, os.cpu_count() or 4)
    params_dict = asdict(params)
    tasks = [(float(p), float(e), params_dict) for p, e in product(ph_axis, pe_axis)]

    results: list[GridPointResult] = []
    done = 0
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(params.dll_path, params.db_path),
    ) as pool:
        for row in pool.map(_worker_eval, tasks):
            results.append(GridPointResult(**row))
            done += 1
            if progress_cb:
                progress_cb(done, total)

    return results
